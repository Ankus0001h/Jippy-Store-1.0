import os
import re
import random
import datetime
import threading
from bson import ObjectId
from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    flash,
    send_from_directory,
    make_response,
)
from pymongo import MongoClient
from twilio.rest import Client as TwilioClient
import cloudinary
import cloudinary.uploader
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, current_user
import uuid
from twilio.twiml.voice_response import VoiceResponse
from datetime import timedelta  # NEW
import pytz
IST = pytz.timezone("Asia/Kolkata")
import qrcode
from io import BytesIO
import base64
from urllib.parse import quote
load_dotenv()

from flask_caching import Cache

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.getenv("SECRET_KEY", "devkey")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=90)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Cache configuration for performance   
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300
cache = Cache(app)

@app.route('/sw.js')
def service_worker():
    response = make_response(send_from_directory('static', 'sw.js', mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/manifest.json')
def web_manifest():
    response = make_response(send_from_directory('static', 'manifest.json', mimetype='application/manifest+json'))
    return response

# ---------- 📒 Register Society Store Credit (Khata) Blueprint ----------
from khata import khata_bp, clean_phone_10, standardize_address_components, reverse_order_store_credit, adjust_order_store_credit, recalculate_khata_user_balance, _resolve_order_khata_record
app.register_blueprint(khata_bp)



# ---------- Security & admin creds ----------
bcrypt = Bcrypt(app)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_NUMBER_1 = os.getenv("ADMIN_NUMBER_1", "")
ADMIN_NUMBER_2 = os.getenv("ADMIN_NUMBER_2", "")

# ---------- 🛡️ Dedicated Admin Session Token (100% Isolated from Customer Logins) ----------
from itsdangerous import URLSafeTimedSerializer

ADMIN_COOKIE_NAME = "jippy_admin_session"
admin_serializer = URLSafeTimedSerializer(app.secret_key, salt="jippy-admin-secret-salt-2026")

def generate_admin_token(email):
    return admin_serializer.dumps({"email": email, "role": "admin"})

def verify_admin_token(token, max_age=365 * 24 * 3600):
    if not token:
        return False
    try:
        data = admin_serializer.loads(token, max_age=max_age)
        return data.get("email") == ADMIN_EMAIL
    except Exception:
        return False

# ---------- 🛡️ Dedicated Vendor Session Token (Remember Login like Admin) ----------
VENDOR_COOKIE_NAME = "jippy_vendor_session"
vendor_serializer = URLSafeTimedSerializer(app.secret_key, salt="jippy-vendor-secret-salt-2026")

def generate_vendor_token(vendor_id, email):
    return vendor_serializer.dumps({"vendor_id": str(vendor_id), "email": str(email), "role": "vendor"})

def verify_vendor_token(token, max_age=365 * 24 * 3600):
    if not token:
        return None
    try:
        data = vendor_serializer.loads(token, max_age=max_age)
        if data.get("role") == "vendor" and data.get("vendor_id"):
            return data
        return None
    except Exception:
        return None

# ---------- Twilio Verify ----------
account_sid = os.environ["TWILIO_ACCOUNT_SID"]
auth_token = os.environ["TWILIO_AUTH_TOKEN"]
verify_sid = os.environ["TWILIO_VERIFY_SERVICE_SID"]
twilio_client = TwilioClient(account_sid, auth_token)  # Twilio client[web:481]
TW_PROXY_NUM = os.getenv("TWILIO_PROXY_NUMBER")
BASE_URL = os.getenv("BASE_URL", "")

# ---------- MongoDB ----------
try:
    import dns.resolver
    dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
    dns.resolver.default_resolver.nameservers = ['8.8.8.8', '1.1.1.1', '8.8.4.4']
except Exception:
    pass

import certifi

MONGO_URI = os.getenv("MONGO_URI")
mongo_client = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where(),
    maxPoolSize=50,
    minPoolSize=0,
    maxIdleTimeMS=45000,
    connectTimeoutMS=30000,
    socketTimeoutMS=45000,
    serverSelectionTimeoutMS=20000,
    retryWrites=True,
    retryReads=True,
)
db = mongo_client["grocery_app"]

# ---------- Firebase Admin ----------
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth, messaging
import json
import os

firebase_json_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

try:
    if not firebase_admin._apps:
        if firebase_json_env:
            # 🚀 Hosting / Production: Read directly from hosting Environment Variables (No file needed on GitHub)
            cred_dict = json.loads(firebase_json_env)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            print("Firebase Admin initialized via FIREBASE_SERVICE_ACCOUNT_JSON environment variable")
        else:
            # 💻 Local Development: Read from local JSON file
            firebase_cred_path = os.getenv("FIREBASE_CRED_PATH")
            if not firebase_cred_path or not os.path.exists(firebase_cred_path):
                project_dir = os.path.dirname(__file__)
                for fname in os.listdir(project_dir):
                    if "firebase-adminsdk" in fname and fname.endswith(".json"):
                        firebase_cred_path = os.path.join(project_dir, fname)
                        break

            if firebase_cred_path and os.path.exists(firebase_cred_path):
                cred = credentials.Certificate(firebase_cred_path)
                firebase_admin.initialize_app(cred)
                print("Firebase Admin initialized via local JSON file")
except Exception as e:
    print(f"Error initializing Firebase Admin: {e}")

# ---------- Fees & thresholds ----------

users = db["users"]
products = db["products"]
orders = db["orders"]
settings = db["settings"]
societies = db["societies"]
categories_col = db["categories"]
admin_push_subscriptions = db["admin_push_subscriptions"]

# High concurrency background indexing
try:
    products.create_index([("category", 1), ("is_active", 1)], background=True)
    products.create_index([("is_active", 1)], background=True)
    orders.create_index([("order_id", 1)], background=True)
    orders.create_index([("vendor_id", 1), ("created_at", -1)], background=True)
    orders.create_index([("items.vendor_id", 1), ("created_at", -1)], background=True)
    orders.create_index([("vendor_status", 1)], background=True)
    orders.create_index([("created_at", -1)], background=True)
    orders.create_index([("user_id", 1), ("created_at", -1)], background=True)
    orders.create_index([("status", 1)], background=True)
    users.create_index([("phone", 1)], background=True)
    societies.create_index([("name", 1)], background=True)
    categories_col.create_index([("normalized_name", 1)], unique=True, background=True)
except Exception as _idx_err:
    print("MongoDB index init warning:", _idx_err)


# ---------- Cloudinary ----------
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

# ---------- Flask-Login (admin/portal use) ----------
login_manager = LoginManager()
login_manager.login_view = "admin_login"
login_manager.init_app(app)


class User(UserMixin):
    def __init__(self, doc):
        self.id = str(doc["_id"])
        self.email = doc.get("email", "")
        self.name = doc.get("name", "")

def build_upi_qr(vpa: str, name: str, amount: float, note: str = "COD Collection"):
    # standard UPI deeplink
    upi_url = (
        "upi://pay?"
        f"pa={quote(vpa)}&"
        f"pn={quote(name)}&"
        f"am={amount:.2f}&"
        "cu=INR&"
        f"tn={quote(note)}"
    )  # [web:120][web:123][web:136]

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}", upi_url

def is_maintenance_mode():
    doc = settings.find_one({"key": "maintenance_mode"}) or {}
    return bool(doc.get("value", False))

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

def generate_order_id() -> str:
    # Example: FK-20250111-AB12CD34
    # हमेशा UTC से date लो ताकि consistent रहे
    date_part = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    random_part = uuid.uuid4().hex[:8].upper()
    return f"FK-{date_part}-{random_part}"

@app.before_request
def make_session_permanent():
    session.permanent = True
    # 🛡️ 100% Dedicated Admin Session Verification:
    # If signed admin cookie is valid, guarantee admin_logged_in is True regardless of customer login/logout
    admin_cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if admin_cookie and verify_admin_token(admin_cookie):
        session["admin_logged_in"] = True

    # 🛡️ 100% Dedicated Vendor Session Verification (Remember Vendor Login like Admin):
    vendor_cookie = request.cookies.get(VENDOR_COOKIE_NAME)
    if vendor_cookie and (session.get("user_role") != "vendor" or not session.get("portal_user_id")):
        vendor_data = verify_vendor_token(vendor_cookie)
        if vendor_data and vendor_data.get("vendor_id"):
            try:
                v_doc = users.find_one({"_id": ObjectId(vendor_data["vendor_id"]), "role": "vendor"})
                if v_doc:
                    session["user_role"] = "vendor"
                    session["user_email"] = v_doc.get("email")
                    session["portal_user_id"] = str(v_doc.get("_id"))
                    session["portal_user_name"] = v_doc.get("name") or v_doc.get("shop_name") or v_doc.get("email")
            except Exception:
                pass

@app.after_request
def add_cache_headers(response):
    """Adds Cache-Control headers to speed up WebView apps and browsers."""
    if request.path.startswith('/static/'):
        # Cache static files (CSS, JS, images) for 30 days
        response.cache_control.max_age = 2592000
        response.cache_control.public = True
    elif request.path.startswith('/category/'):
        # Cache main public pages for a short time (e.g., 5 mins) if they don't have user specific data
        # BUT just to be safe with user carts, we'll let the frontend revalidate them.
        # WebView can cache them with must-revalidate.
        response.cache_control.max_age = 300
        response.cache_control.public = True
    else:
        # Prevent caching for dynamic/API/admin routes
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
        response.cache_control.must_revalidate = True
        response.cache_control.max_age = 0
    return response


@app.template_filter('ist_time')
def ist_time_filter(dt):
    if dt is None:
        return ""
    # अगर tzinfo नहीं है तो UTC मान लो
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST).strftime('%d %b %Y, %I:%M %p')

def twilio_send_otp_dummy(phone: str) -> str:
    pass

def normalize_category_name(name: str) -> str:
    """
    Case-insensitive, space/punctuation-insensitive normalization
    so 'fruits', 'Fruits ', 'FRUITS', ' fruits ' sab ek hi ban jaye.
    """
    if not name:
        return ""
    # lower + strip
    name = name.strip().lower()
    # continuous spaces ko single space karo
    name = re.sub(r"\s+", " ", name)
    return name

def twilio_check_otp_dummy(phone: str, code: str) -> bool:
    pass

def normalize_phone(raw_phone: str) -> str:
    p = re.sub(r"\D", "", raw_phone)  # non-digits hatao
    # assume India 10-digit
    if len(p) == 10:
        return "+91" + p
    elif p.startswith("91") and len(p) == 12:
        return "+" + p
    elif p.startswith("+"):
        return p
    else:
        return "+91" + p  # fallback


@login_manager.user_loader
def load_user(user_id):
    doc = users.find_one({"_id": ObjectId(user_id)})
    return User(doc) if doc else None


@app.context_processor
def inject_user():
    cat_imgs = {}
    try:
        for doc in categories_col.find({"image": {"$exists": True, "$ne": ""}}):
            img_url = doc.get("image", "")
            if img_url:
                if doc.get("name"):
                    cat_imgs[doc["name"].lower().strip()] = img_url
                if doc.get("normalized_name"):
                    cat_imgs[doc["normalized_name"]] = img_url
    except Exception as e:
        pass

    # Check store closed state globally for all templates
    is_shop_closed = False
    try:
        shop_id = session.get("current_shop_id") or session.get("portal_user_id")
        shop_doc = None
        if shop_id:
            try:
                shop_doc = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
            except Exception:
                shop_doc = None
        if not shop_doc:
            current_society_id = session.get("current_society_id")
            if current_society_id:
                try:
                    soc_oid = ObjectId(current_society_id)
                    shop_doc = users.find_one({
                        "role": "vendor",
                        "$or": [
                            {"serviceable_societies": str(current_society_id)},
                            {"serviceable_societies": soc_oid}
                        ]
                    })
                except Exception:
                    shop_doc = users.find_one({"role": "vendor", "serviceable_societies": str(current_society_id)})
            if not shop_doc:
                shop_doc = users.find_one({"role": "vendor"}, sort=[("_id", -1)])
        if shop_doc:
            val = shop_doc.get("is_online")
            if val is False or str(val).lower() in ["false", "0", "offline"]:
                is_shop_closed = True
    except Exception:
        pass

    return dict(current_user=current_user, category_images=cat_imgs, is_shop_closed=is_shop_closed)

# ================================
# PUBLIC: PRODUCTS
# ================================

# TOP LEVEL FUNCTION (app.py ke top mein add karo)
import urllib.parse
import re
import random
from flask import Flask, render_template, request

# Helper function to clean category names
def clean_cat_name(name):
    if not name: return ""
    # Flask path converter handles %20, but sometimes '+' remains
    # and we need to handle potential double encoding from logs (%2520)
    decoded = urllib.parse.unquote(name)
    if "%" in decoded: # Handle double encoding if it exists
        decoded = urllib.parse.unquote(decoded)
    return decoded.replace('+', ' ').strip()

def clean_category_title(name: str) -> str:
    """
    Cleanly capitalizes category names while preserving symbols.
    e.g. 'fresh fruits & vegetables' -> 'Fresh Fruits & Vegetables'
    """
    if not name:
        return ""
    cleaned = re.sub(r"\s+", " ", name.strip())
    words = cleaned.split(" ")
    titled = []
    minor_words = {"and", "&", "of", "in", "the", "for", "to"}
    for i, w in enumerate(words):
        if i > 0 and w.lower() in minor_words:
            titled.append(w.lower() if w != "&" else "&")
        else:
            titled.append(w.capitalize())
    return " ".join(titled)

def validate_category_name(raw_name: str):
    """
    Validates category name with strict conditions & checks.
    Returns: (is_valid: bool, cleaned_name: str, error_message: str)
    """
    if not raw_name or not isinstance(raw_name, str):
        return False, "", "Category name cannot be empty."
    
    cleaned = raw_name.strip()
    if len(cleaned) < 2:
        return False, "", "Category name must be at least 2 characters long."
    if len(cleaned) > 50:
        return False, "", "Category name cannot exceed 50 characters."
    
    # Check for at least one alphabet
    if not re.search(r"[a-zA-Z]", cleaned):
        return False, "", "Category name must contain at least one letter."
        
    # Check for disallowed characters (no HTML, quotes, backslashes, scripts)
    if not re.match(r"^[a-zA-Z0-9\s&/,\-()]+$", cleaned):
        return False, "", "Category name contains invalid characters. Use letters, numbers, spaces, &, /, -, or ()."
        
    formatted = clean_category_title(cleaned)
    return True, formatted, ""

@cache.memoize(timeout=600)
def get_cached_categories():
    try:
        deleted_norms = set(
            doc.get("normalized_name")
            for doc in categories_col.find({"deleted": True})
            if doc.get("normalized_name")
        )
        from_products = set(c.strip() for c in products.distinct("category") if c and isinstance(c, str) and c.strip())
        from_col = set(doc.get("name", "").strip() for doc in categories_col.find({"deleted": {"$ne": True}}) if doc.get("name", "").strip())
        standard = {"Fruits", "Vegetables", "Dairy", "Bakery", "Snacks", "Beverages", "Meat", "Household", "Personal Care"}
        all_cats = from_products.union(from_col).union(standard)
        # Deduplicate case-insensitively while preserving nice titles
        seen = {}
        for c in sorted(all_cats):
            norm = normalize_category_name(c)
            if norm not in deleted_norms:
                if norm not in seen:
                    seen[norm] = clean_category_title(c)
        return sorted(list(seen.values()), key=lambda x: x.lower())
    except Exception as e:
        print(f"Categories cache error: {e}")
        return ["Bakery", "Beverages", "Dairy", "Fruits", "Household", "Meat", "Personal Care", "Snacks", "Vegetables"]

@cache.memoize(timeout=300)
def get_cached_category_items(original_category):
    query = {"category": original_category}
    items = list(products.find(query).limit(40))
    if not items:
        items = list(products.find({
            "category": {"$regex": f"^{re.escape(original_category)}$", "$options": "i"}
        }).limit(40))
        
    for item in items:
        item["_id"] = str(item["_id"])
        item["display_price"] = item.get("discount_price") or item.get("price", 0)
    return items

def group_and_sort_products(items):
    """
    Groups products with the same name into a single product card with multiple variants
    (Zepto / Blinkit style). Then sorts available products first, sold out last.
    """
    groups = {}
    for it in items:
        is_oos = bool(it.get("out_of_stock", False))
        if not is_oos and it.get("stock") is not None:
            try:
                if float(it.get("stock")) <= 0:
                    is_oos = True
            except (ValueError, TypeError):
                pass
        
        it["_id"] = str(it["_id"])
        it["out_of_stock"] = is_oos
        
        price = float(it.get("price", 0) or 0)
        disc = it.get("discount_price")
        if disc is not None:
            try:
                disc = float(disc)
                if disc >= price or disc <= 0:
                    disc = None
            except (ValueError, TypeError):
                disc = None
        it["price"] = price
        it["discount_price"] = disc
        it["display_price"] = disc if disc is not None else price
        
        name_key = it.get("name", "").strip().lower()
        if name_key not in groups:
            groups[name_key] = {
                "name": it.get("name", "").strip(),
                "category": it.get("category", ""),
                "image": it.get("image", ""),
                "slug": it.get("slug", ""),
                "variants": []
            }
        groups[name_key]["variants"].append(it)
    
    result = []
    for g in groups.values():
        variants = g["variants"]
        # Sort variants by in-stock first, then display price ascending
        variants.sort(key=lambda v: (v.get("out_of_stock", False), float(v.get("display_price", 0))))
        
        default_variant = next((v for v in variants if not v.get("out_of_stock")), variants[0])
        g["default_variant"] = default_variant
        g["_id"] = default_variant["_id"]
        g["slug"] = default_variant.get("slug")
        g["price"] = default_variant.get("price", 0)
        g["discount_price"] = default_variant.get("discount_price")
        g["display_price"] = default_variant.get("display_price", 0)
        g["unit"] = default_variant.get("unit", "")
        g["out_of_stock"] = all(v.get("out_of_stock", False) for v in variants)
        g["has_multiple_variants"] = len(variants) > 1
        result.append(g)
    
    in_stock = [p for p in result if not p["out_of_stock"]]
    out_of_stock = [p for p in result if p["out_of_stock"]]
    random.shuffle(in_stock)
    return in_stock + out_of_stock

sort_available_first = group_and_sort_products

@app.route("/category/<path:name>")
def category_page(name):
    try:
        # Step 1: Clean the name
        original_category = clean_cat_name(name)
        
        # Categories fetch (Cached)
        categories = get_cached_categories()
        
        # Fetch Shop with society/default fallback
        shop = None
        shop_id = session.get("current_shop_id")
        if shop_id:
            try:
                shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
            except Exception:
                shop = None

        if not shop:
            current_society_id = session.get("current_society_id")
            if current_society_id:
                try:
                    shop = users.find_one({"role": "vendor", "serviceable_societies": str(current_society_id)})
                except Exception:
                    shop = None
            if not shop:
                shop = users.find_one({"role": "vendor"})

        if shop and not session.get("current_shop_id"):
            session["current_shop_id"] = str(shop.get("_id"))
            session["current_shop_name"] = shop.get("shop_name", "Local Shop")

        # Check for search query parameter
        search_query = request.args.get("search", "").strip()

        # If search query is present
        if search_query:
            regex_q = {"$regex": re.escape(search_query), "$options": "i"}
            matched_items = []
            # If user was inside a specific category, check that category first
            if original_category.lower() not in ["categories", "all"]:
                matched_items = list(products.find({
                    "category": {"$regex": f"^{re.escape(original_category)}$", "$options": "i"},
                    "name": regex_q
                }).limit(60))
            
            # If nothing found in category or user searched globally
            if not matched_items:
                matched_items = list(products.find({
                    "$or": [{"name": regex_q}, {"category": regex_q}]
                }).limit(60))

            for it in matched_items:
                it["_id"] = str(it["_id"])
                it["display_price"] = it.get("discount_price") or it.get("price", 0)
            items = sort_available_first(matched_items)

            return render_template("products.html",
                                 page_type="products",
                                 categories=categories,
                                 items=items,
                                 selected_category=f"Results for '{search_query}'",
                                 item_count=len(items),
                                 search_query=search_query,
                                 shop=shop)

        # If user selected 'categories' or 'all' with no search, display categories overview
        if original_category.lower() in ["categories", "all"]:
            return render_template("products.html",
                                 page_type="categories",
                                 categories=categories,
                                 search_query="",
                                 shop=shop)

        # Step 2: Database Query (Cached)
        items = get_cached_category_items(original_category)
        
        # Place available products first, sold out items at the end
        items = sort_available_first(list(items))

        print(f"[OK] Category='{original_category}', Found {len(items)} items")

        is_shop_closed = False
        if shop:
            val = shop.get("is_online")
            if val is False or str(val).lower() in ["false", "0", "offline"]:
                is_shop_closed = True

        return render_template("products.html",
                             page_type="products",
                             categories=categories,
                             items=items,
                             selected_category=original_category,
                             item_count=len(items),
                             search_query="",
                             shop=shop,
                             is_shop_closed=is_shop_closed)
    except Exception as e:
        print(f"[ERROR] Category error: {str(e)}")
        return render_template("products.html", items=[], categories=[], item_count=0, search_query="", shop=None, is_shop_closed=False)


@app.route("/")
def index():
    force = request.args.get("force")
    if force:
        session.pop("current_shop_id", None)
        session.pop("current_society_id", None)
        session.pop("current_shop_name", None)
        session.pop("current_society_name", None)
        return render_template("location.html")

    # 1. If user is already inside a store/shop, stay in their store home!
    shop_id = session.get("current_shop_id")
    if shop_id:
        try:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
            if shop:
                return redirect(url_for("shop_home", shop_id=str(shop_id)))
        except Exception:
            pass

    # 2. If user already chose a society, show that society's shops
    society_id = session.get("current_society_id")
    if society_id:
        try:
            society = societies.find_one({"_id": ObjectId(society_id)})
            if society:
                return redirect(url_for("society_shops", society_id=str(society_id)))
        except Exception:
            pass

    # If only 1 society exists in DB, auto-direct to that society's store!
    if societies.count_documents({}) == 1:
        single_soc = societies.find_one()
        if single_soc:
            return redirect(url_for("society_shops", society_id=str(single_soc["_id"])))

    # 3. If user has location coordinates, show nearby societies
    if session.get("user_lat") and session.get("user_lng"):
        return redirect(url_for("societies_page"))

    # 4. Otherwise, show location fetcher page
    return render_template("location.html")

@app.route("/set-location", methods=["POST"])
def set_location():
    data = request.json
    session["user_lat"] = data.get("lat")
    session["user_lng"] = data.get("lng")
    session["user_address"] = data.get("address")
    session.pop("current_society_id", None)
    session.pop("current_society_name", None)
    session.pop("current_shop_id", None)
    session.pop("current_shop_name", None)
    return jsonify({"status": "success"})

@app.route("/societies")
def societies_page():
    user_address = session.get("user_address", "Your Location")
    query = request.args.get("search", "").strip()
    
    results = []
    if query:
        # Manual Search
        regex_q = {"$regex": query, "$options": "i"}
        socs = list(societies.find({
            "$or": [
                {"city": regex_q},
                {"pincode": regex_q},
                {"name": regex_q}
            ]
        }).limit(20))
        
        for soc in socs:
            results.append({
                "id": str(soc["_id"]),
                "name": soc.get("name"),
                "city": soc.get("city"),
                "pincode": soc.get("pincode"),
                "image": soc.get("image"),
                "distance": None
            })
        top_societies = results
        user_address = f"Search: {query}"
        return render_template("societies.html", societies=top_societies, user_address=user_address)
    else:
        # GPS Search
        user_lat = session.get("user_lat")
        user_lng = session.get("user_lng")
        if not user_lat or not user_lng:
            return redirect(url_for("index"))
            
        all_socs = list(societies.find())
        for soc in all_socs:
            sloc = soc.get("location", {})
            slat = sloc.get("lat")
            slng = sloc.get("lng")
            
            if slat and slng:
                dist = haversine_distance(float(user_lat), float(user_lng), slat, slng)
                results.append({
                    "id": str(soc["_id"]),
                    "name": soc.get("name"),
                    "city": soc.get("city"),
                    "pincode": soc.get("pincode"),
                    "image": soc.get("image"),
                    "distance": round(dist, 1)
                })
                
        # Sort by distance
        results.sort(key=lambda x: x["distance"])
        
        # Max service radius check (5 km limit for delivery zone)
        MAX_SERVICE_RADIUS_KM = 5.0
        in_range_societies = [s for s in results if s["distance"] is not None and s["distance"] <= MAX_SERVICE_RADIUS_KM]
        nearest_soc = results[0] if results else None
        
        return render_template(
            "societies.html",
            societies=in_range_societies if in_range_societies else [],
            nearest_society=nearest_soc,
            user_address=user_address,
            max_radius=MAX_SERVICE_RADIUS_KM
        )

@app.route("/society/<society_id>/shops")
def society_shops(society_id):
    try:
        # Find society
        society = societies.find_one({"_id": ObjectId(society_id)})
        if not society:
            return "Society not found", 404
            
        session["current_society_id"] = str(society_id)
        session["current_society_name"] = society.get("name")
        session["current_society_city"] = society.get("city")
        session["current_society_pincode"] = society.get("pincode")
        sloc = society.get("location", {})
        session["current_society_lat"] = sloc.get("lat")
        session["current_society_lng"] = sloc.get("lng")
            
        # Find vendors that serve this society
        # Vendors should have society_id string or ObjectId in their serviceable_societies list
        try:
            soc_oid = ObjectId(society_id)
            query_filter = {
                "role": "vendor",
                "$or": [
                    {"serviceable_societies": str(society_id)},
                    {"serviceable_societies": soc_oid}
                ]
            }
        except Exception:
            query_filter = {
                "role": "vendor",
                "serviceable_societies": str(society_id)
            }

        shops = list(users.find(query_filter))
        
        current_shop_id = session.get("current_shop_id")
        if current_shop_id and not any(str(s["_id"]) == str(current_shop_id) for s in shops):
            session.pop("current_shop_id", None)
            session.pop("current_shop_name", None)

        return render_template("shops.html", society=society, shops=shops)
    except Exception as e:
        print(f"Error in society_shops: {e}")
        return redirect(url_for("societies_page"))

@app.route("/shop/<shop_id>")
def shop_home(shop_id):
    shop = None
    try:
        # Check if shop exists
        shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        if not shop:
            return "Shop not found", 404
            
        session["current_shop_id"] = str(shop_id)
        session["current_shop_name"] = shop.get("shop_name", "Local Shop")
        shop_img = shop.get("shop_image") or shop.get("image")
        if shop_img:
            session["current_shop_image"] = shop_img
        else:
            session.pop("current_shop_image", None)
        
        is_shop_closed = False
        if shop:
            val = shop.get("is_online")
            if val is False or str(val).lower() in ["false", "0", "offline"]:
                is_shop_closed = True
        
        categories = get_cached_categories()
        return render_template("products.html", page_type="categories", categories=categories, shop=shop, is_shop_closed=is_shop_closed)
    except Exception as e:
        print(f"Error in shop_home: {e}")
        try:
            categories = get_cached_categories()
        except Exception:
            categories = ["Fruits & Vegetables", "Dairy & Breakfast", "Munchies & Snacks", "Cold Drinks & Juices", "Instant & Frozen Food", "Tea, Coffee & Health Drinks", "Bakery & Biscuits", "Sweet Tooth", "Atta, Rice & Dal", "Dry Fruits & Masala", "Sauces & Spreads", "Chicken, Meat & Fish", "Pan Corner", "Organic & Premium", "Baby Care", "Pharma & Wellness", "Cleaning Essentials", "Home & Office", "Personal Care", "Pet Care"]
        return render_template("products.html", page_type="categories", categories=categories, shop=shop, is_shop_closed=False)
    
# ================================
# MOBILE APP JSON APIs
# ================================

@app.route("/api/societies")
def api_societies():
    query = request.args.get("search", "").strip()
    results = []
    if query:
        regex_q = {"$regex": query, "$options": "i"}
        socs = list(societies.find({"$or": [{"city": regex_q}, {"pincode": regex_q}, {"name": regex_q}]}).limit(20))
    else:
        socs = list(societies.find().limit(20))
        
    for soc in socs:
        results.append({
            "id": str(soc["_id"]),
            "name": soc.get("name"),
            "city": soc.get("city"),
            "pincode": soc.get("pincode"),
            "image": soc.get("image")
        })
    return jsonify({"status": "success", "societies": results})

@app.route("/api/society/<society_id>/shops")
def api_society_shops(society_id):
    try:
        soc_oid = ObjectId(society_id)
        query_filter = {
            "role": "vendor",
            "$or": [
                {"serviceable_societies": str(society_id)},
                {"serviceable_societies": soc_oid}
            ]
        }
    except Exception:
        query_filter = {"role": "vendor", "serviceable_societies": str(society_id)}

    shops_data = list(users.find(query_filter))
    results = []
    for s in shops_data:
        results.append({
            "id": str(s["_id"]),
            "shop_name": s.get("shop_name", "Shop"),
            "owner_name": s.get("name", ""),
            "contact": s.get("phone", ""),
            "shop_image": s.get("shop_image") or s.get("image", ""),
            "vendor_type": s.get("vendor_type", "")
        })
    return jsonify({"status": "success", "shops": results})

@app.route("/api/shop/<shop_id>/categories")
def api_shop_categories(shop_id):
    shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
    if not shop:
        return jsonify({"status": "error", "message": "Shop not found"}), 404
    categories = get_cached_categories()
    cats_data = [{"id": str(idx), "name": c, "image": ""} for idx, c in enumerate(categories)]
    return jsonify({"status": "success", "shop_name": shop.get("shop_name"), "categories": cats_data})

@app.route("/api/shop/<shop_id>/category/<path:category_name>")
def api_shop_category_products(shop_id, category_name):
    shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
    if not shop:
        return jsonify({"status": "error", "message": "Shop not found"}), 404
        
    try:
        original_category = clean_cat_name(category_name)
        items = get_cached_category_items(original_category)
        items = sort_available_first(list(items))
        
        products_data = []
        shop_id_str = str(shop_id)
        for p in items:
            oos_vendors = [str(v) for v in p.get("out_of_stock_vendors", [])]
            is_oos = bool(p.get("out_of_stock", False) or (shop_id_str in oos_vendors))
            products_data.append({
                "id": str(p["_id"]),
                "name": p.get("name", ""),
                "price": p.get("price", 0),
                "discount_price": p.get("discount_price") or p.get("price", 0),
                "unit": p.get("unit", ""),
                "image": p.get("image", ""),
                "slug": p.get("slug", ""),
                "out_of_stock": is_oos
            })
            
        return jsonify({
            "status": "success", 
            "products": products_data,
            "shop_is_online": shop.get("is_online", True)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Common Hindi & English Grocery Synonyms for Smart Search
GROCERY_SYNONYMS = {
    "dahi": ["curd", "dahi", "yogurt"],
    "curd": ["dahi", "curd"],
    "doodh": ["milk", "doodh"],
    "milk": ["milk", "doodh", "dairy"],
    "makhan": ["butter", "makhan", "amul"],
    "butter": ["butter", "makhan", "amul"],
    "aata": ["aata", "atta", "wheat", "flour"],
    "atta": ["aata", "atta", "wheat", "flour"],
    "chawal": ["rice", "chawal"],
    "rice": ["rice", "chawal"],
    "cheeni": ["sugar", "cheeni"],
    "sugar": ["sugar", "cheeni"],
    "tel": ["oil", "mustard", "refined"],
    "oil": ["oil", "refined", "mustard"],
    "chai": ["tea", "chai"],
    "tea": ["tea", "chai"],
    "biscuit": ["biscuit", "cookie", "parle", "britannia"],
    "biscuits": ["biscuit", "cookie"],
    "maggi": ["maggi", "noodle", "instant food"],
    "noodles": ["noodle", "maggi"],
    "cold drink": ["soft drinks", "coke", "pepsi", "sprite", "juice"],
    "cold drinks": ["soft drinks", "coke", "pepsi", "sprite", "juice"],
    "soda": ["soda", "drink"],
    "sabzi": ["vegetable", "sabzi"],
    "namkeen": ["namkeen", "snack", "chips", "bhujia"],
    "chips": ["chips", "lays", "kurkure", "snack"],
    "surf": ["detergent", "surf", "washing powder"],
    "soap": ["soap", "bath", "dettol", "lifebuoy"]
}

@app.route("/search")
def search_page():
    q = request.args.get("q", "").strip()
    if not q:
        return redirect(url_for("category_page", name="categories"))
    return redirect(url_for("category_page", name="all", search=q))

@app.route("/api/suggestions")
def api_suggestions():
    # Support full catalog pre-load for instant 0ms offline/WebView search
    if request.args.get("all") == "1":
        try:
            prods = list(products.find({}, {
                "name": 1,
                "category": 1,
                "price": 1,
                "discount_price": 1,
                "unit": 1,
                "image": 1,
                "out_of_stock": 1
            }).limit(300))
            catalog = []
            for p in prods:
                p_name = p.get("name", "").strip()
                if not p_name:
                    continue
                catalog.append({
                    "name": p_name,
                    "category": p.get("category", ""),
                    "price": p.get("discount_price") or p.get("price", 0),
                    "orig_price": p.get("price", 0) if p.get("discount_price") else None,
                    "unit": p.get("unit", ""),
                    "image": p.get("image", ""),
                    "out_of_stock": bool(p.get("out_of_stock")),
                    "url": f"/category/{quote(p.get('category', 'all'))}?search={quote(p_name)}"
                })
            return jsonify(catalog)
        except Exception as e:
            print(f"Catalog suggestions error: {e}")
            return jsonify([])

    q = request.args.get("q", "").strip()
    if not q:
        trending = ["Milk", "Amul Butter", "Maggi", "Cold Drinks", "Aata", "Chips", "Bread", "Dahi", "Biscuits"]
        return jsonify([{"name": t, "type": "trending", "url": f"/category/all?search={quote(t)}"} for t in trending])

    q_lower = q.lower()
    results = []
    seen = set()

    search_terms = [q]
    if q_lower in GROCERY_SYNONYMS:
        search_terms.extend(GROCERY_SYNONYMS[q_lower])

    # 1. Match Categories (e.g., Soft Drinks, Dairy Products)
    try:
        all_cats = get_cached_categories()
        for cat in all_cats:
            cat_l = cat.lower()
            if any(term.lower() in cat_l for term in search_terms):
                if cat_l not in seen:
                    seen.add(cat_l)
                    results.append({
                        "name": cat,
                        "type": "category",
                        "url": f"/category/{urllib.parse.quote(cat)}"
                    })
    except Exception:
        pass

    # 2. Match Products across database with price and image
    try:
        regex_patterns = []
        for term in search_terms:
            reg = {"$regex": re.escape(term), "$options": "i"}
            regex_patterns.append({"name": reg})
            regex_patterns.append({"category": reg})
            
        matched_products = list(products.find({"$or": regex_patterns}).limit(20))
        for p in matched_products:
            p_name = p.get("name", "").strip()
            p_name_l = p_name.lower()
            if p_name_l and p_name_l not in seen:
                seen.add(p_name_l)
                p_cat = p.get("category", "")
                p_price = p.get("discount_price") or p.get("price", 0)
                p_img = p.get("image", "")
                results.append({
                    "name": p_name,
                    "type": "product",
                    "category": p_cat,
                    "price": p_price,
                    "unit": p.get("unit", ""),
                    "image": p_img,
                    "url": f"/category/all?search={urllib.parse.quote(p_name)}"
                })
                if len(results) >= 10:
                    break
    except Exception as e:
        print(f"Suggestions API error: {e}")

    return jsonify(results[:10])

# ================================
# ADMIN: AUTH & DASHBOARD
# ================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        admin_cookie = request.cookies.get(ADMIN_COOKIE_NAME)
        if admin_cookie and verify_admin_token(admin_cookie):
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        captcha_code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["captcha_code"] = captcha_code
        return render_template("admin_login.html", captcha_code=captcha_code)

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    captcha_input = request.form.get("captcha", "").strip()

    stored_captcha = session.get("captcha_code", "")
    if not stored_captcha or captcha_input != stored_captcha:
        new_captcha = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["captcha_code"] = new_captcha
        return render_template(
            "admin_login.html",
            error="Invalid CAPTCHA. Please try again.",
            captcha_code=new_captcha,
        )

    session.pop("captcha_code", None)

    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        session.permanent = True
        session["admin_logged_in"] = True
        token = generate_admin_token(email)
        resp = redirect(url_for("admin_dashboard"))
        resp.set_cookie(
            ADMIN_COOKIE_NAME,
            value=token,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            path="/"
        )
        return resp

    new_captcha = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["captcha_code"] = new_captcha
    return render_template(
        "admin_login.html",
        error="Invalid email or password.",
        captcha_code=new_captcha,
    )


@app.route("/admin/refresh-captcha", methods=["POST"])
def refresh_captcha():
    captcha_code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["captcha_code"] = captcha_code
    return jsonify({"captcha_code": captcha_code})


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    resp = redirect(url_for("admin_login"))
    resp.delete_cookie(ADMIN_COOKIE_NAME, path="/")
    return resp


# ================================
# ADMIN: ADD / EDIT PRODUCT
# ================================

@app.route("/admin/api/add-category", methods=["POST"])
def admin_api_add_category():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Unauthorized. Please log in as admin."}), 401

    raw_name = request.form.get("name", "").strip()
    if not raw_name and request.is_json:
        raw_name = (request.get_json(silent=True) or {}).get("name", "").strip()

    is_valid, cleaned_name, err = validate_category_name(raw_name)
    if not is_valid:
        return jsonify({"success": False, "message": err}), 400

    norm_name = normalize_category_name(cleaned_name)
    existing_cats = get_cached_categories()
    existing_map = {normalize_category_name(c): c for c in existing_cats}

    # Handle Category Image Upload (Cloudinary)
    cat_image_url = ""
    cat_image_public_id = ""
    if "image" in request.files:
        cat_file = request.files["image"]
        if cat_file and cat_file.filename != "":
            allowed_exts = {"jpg", "jpeg", "png", "webp", "gif"}
            ext = cat_file.filename.rsplit(".", 1)[-1].lower() if "." in cat_file.filename else ""
            if ext in allowed_exts:
                try:
                    upload_res = cloudinary.uploader.upload(cat_file, folder="categories")
                    cat_image_url = upload_res.get("secure_url", "")
                    cat_image_public_id = upload_res.get("public_id", "")
                except Exception as up_err:
                    print(f"Category image upload error: {up_err}")

    update_payload = {
        "name": cleaned_name,
        "normalized_name": norm_name,
        "created_at": datetime.datetime.now(IST)
    }
    if cat_image_url:
        update_payload["image"] = cat_image_url
        update_payload["image_public_id"] = cat_image_public_id

    try:
        categories_col.update_one(
            {"normalized_name": norm_name},
            {"$set": update_payload},
            upsert=True
        )
        cache.delete_memoized(get_cached_categories)
    except Exception as e:
        print(f"Error saving category: {e}")

    updated_cats = get_cached_categories()

    if norm_name in existing_map:
        matched = existing_map[norm_name]
        return jsonify({
            "success": True,
            "category": matched,
            "image": cat_image_url,
            "message": f"Category '{matched}' updated successfully!",
            "is_existing": True,
            "categories": updated_cats
        })

    return jsonify({
        "success": True,
        "category": cleaned_name,
        "image": cat_image_url,
        "message": f"Category '{cleaned_name}' added successfully!",
        "is_existing": False,
        "categories": updated_cats
    })

@app.route("/admin/api/categories", methods=["GET"])
def admin_api_list_categories():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    cats = get_cached_categories()
    result = []
    for c in cats:
        norm = normalize_category_name(c)
        doc = categories_col.find_one({"normalized_name": norm, "deleted": {"$ne": True}})
        img = doc.get("image", "") if doc else ""
        cnt = products.count_documents({"category": {"$regex": f"^{re.escape(c)}$", "$options": "i"}})
        result.append({
            "name": c,
            "image": img,
            "product_count": cnt
        })
    return jsonify({"success": True, "categories": result})

@app.route("/admin/api/delete-category", methods=["POST"])
def admin_api_delete_category():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Unauthorized. Please log in as admin."}), 401

    raw_name = request.form.get("category", "").strip()
    if not raw_name and request.is_json:
        raw_name = (request.get_json(silent=True) or {}).get("category", "").strip()

    if not raw_name:
        return jsonify({"success": False, "message": "Category name is required."}), 400

    norm_name = normalize_category_name(raw_name)

    # Check if active products are assigned to this category
    product_count = products.count_documents({
        "category": {"$regex": f"^{re.escape(raw_name)}$", "$options": "i"}
    })
    if product_count > 0:
        return jsonify({
            "success": False,
            "message": f"Cannot delete '{raw_name}' because {product_count} product(s) are currently assigned to it. Please reassign or delete those products first."
        }), 400

    # Delete category image from Cloudinary if stored
    try:
        cat_doc = categories_col.find_one({"normalized_name": norm_name})
        if cat_doc:
            pub_id = cat_doc.get("image_public_id")
            if pub_id:
                try:
                    cloudinary.uploader.destroy(pub_id, invalidate=True)
                except Exception:
                    pass
            # Mark deleted or remove
            categories_col.update_one(
                {"normalized_name": norm_name},
                {"$set": {"deleted": True, "deleted_at": datetime.datetime.now(IST)}},
                upsert=True
            )
        else:
            categories_col.insert_one({
                "name": raw_name,
                "normalized_name": norm_name,
                "deleted": True,
                "deleted_at": datetime.datetime.now(IST)
            })

        cache.delete_memoized(get_cached_categories)
    except Exception as e:
        print(f"Error deleting category: {e}")
        return jsonify({"success": False, "message": f"Database error: {str(e)}"}), 500

    updated_cats = get_cached_categories()
    return jsonify({
        "success": True,
        "message": f"Category '{raw_name}' deleted successfully.",
        "category": raw_name,
        "categories": updated_cats
    })


@app.route("/admin/add-product", methods=["GET", "POST"])
def add_product():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    categories_list = get_cached_categories()

    if request.method == "POST":
        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.form.get("is_ajax") == "1"
            or "application/json" in request.headers.get("Accept", "")
        )

        name = request.form.get("name", "").strip()
        price_raw = request.form.get("price", "").strip()
        discount_price_raw = request.form.get("discount_price", "").strip()
        unit = request.form.get("unit", "").strip()
        category = request.form.get("category", "").strip()
        other_category = request.form.get("other_category", "").strip()
        brand = request.form.get("brand", "").strip()
        description = request.form.get("description", "").strip()
        out_of_stock = request.form.get("out_of_stock") in ("1", "true", "on", "yes")

        def handle_error(msg):
            if is_ajax:
                return jsonify({"success": False, "message": msg}), 400
            products_list = list(products.find().sort("_id", -1))
            return render_template(
                "add_product.html",
                error=msg,
                products=products_list,
                categories=categories_list,
                form_data=request.form,
            )

        # 1. Product Name Validation
        if not name or len(name) < 2:
            return handle_error("Product Title must be at least 2 characters long.")

        # 2. Packaging Unit Validation
        if not unit:
            return handle_error("Packaging Unit (e.g. 1 kg, 500 g, 1 Pc) is required.")

        # 3. Category Validation & Resolution
        target_cat = other_category if category == "Other" else category
        if not target_cat:
            return handle_error("Please select or enter a valid category.")

        is_valid_cat, cleaned_cat, cat_err = validate_category_name(target_cat)
        if not is_valid_cat:
            return handle_error(f"Invalid Category: {cat_err}")

        final_category = cleaned_cat
        # Save custom category if new
        try:
            norm_c = normalize_category_name(final_category)
            categories_col.update_one(
                {"normalized_name": norm_c},
                {"$set": {"name": final_category, "normalized_name": norm_c, "updated_at": datetime.datetime.now(IST)}},
                upsert=True
            )
            cache.delete_memoized(get_cached_categories)
            categories_list = get_cached_categories()
        except Exception:
            pass

        # 4. Listing Price (MRP) Validation
        try:
            price = float(price_raw)
            if price <= 0:
                raise ValueError("Price must be strictly positive.")
            if price > 1000000:
                raise ValueError("Price exceeds reasonable catalog limit.")
        except (ValueError, TypeError):
            return handle_error("Listing Price (MRP) must be a valid number greater than ₹0.")

        # 5. Discount / Selling Price Validation (Policy: Selling Price <= MRP)
        discount_price = None
        if discount_price_raw:
            try:
                discount_price = float(discount_price_raw)
                if discount_price <= 0:
                    raise ValueError("Discounted price must be greater than ₹0.")
                if discount_price > price:
                    return handle_error(f"Selling Price (₹{discount_price:.2f}) cannot exceed MRP (₹{price:.2f}).")
            except (ValueError, TypeError):
                return handle_error("Please enter a valid discounted price or leave it empty.")

        # 6. Image File Validation & Upload (Supports reusing image for variants)
        existing_image_url = request.form.get("existing_image_url", "").strip()
        existing_image_public_id = request.form.get("existing_image_public_id", "").strip()

        file = request.files.get("image")
        final_image_url = None
        final_public_id = ""

        if file and file.filename != "":
            allowed_exts = {"jpg", "jpeg", "png", "webp", "gif"}
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in allowed_exts:
                return handle_error("Invalid image format. Supported formats: JPG, PNG, WEBP, GIF.")

            try:
                upload_result = cloudinary.uploader.upload(file, folder="products")
                final_image_url = upload_result["secure_url"]
                final_public_id = upload_result.get("public_id", "")
            except Exception as e:
                return handle_error(f"Image upload failed: {str(e)}")
        elif existing_image_url and (existing_image_url.startswith("http://") or existing_image_url.startswith("https://")):
            final_image_url = existing_image_url
            final_public_id = existing_image_public_id
        else:
            return handle_error("Product showcase image is required.")

        # 7. Unique Slug Generation
        base_slug = slugify(name + "-" + unit)
        slug = base_slug
        if products.find_one({"slug": slug}):
            slug = f"{base_slug}-{int(price)}"
            if products.find_one({"slug": slug}):
                slug = f"{base_slug}-{uuid.uuid4().hex[:4]}"

        # 8. Create Product Document
        doc = {
            "name": name,
            "slug": slug,
            "price": float(price),
            "unit": unit,
            "category": final_category,
            "image": final_image_url,
            "image_public_id": final_public_id,
            "out_of_stock": bool(out_of_stock),
            "created_at": datetime.datetime.now(IST),
        }

        if discount_price is not None:
            doc["discount_price"] = float(discount_price)
        if brand:
            doc["brand"] = brand
        if description:
            doc["description"] = description

        products.insert_one(doc)

        try:
            cache.delete_memoized(get_cached_categories)
        except Exception:
            pass

        if is_ajax:
            return jsonify({
                "success": True,
                "message": f"'{name}' ({unit}) published successfully to Jippy Store!",
                "product_id": str(doc["_id"]),
                "slug": slug,
                "name": name,
                "unit": unit,
                "category": final_category,
                "brand": brand,
                "description": description,
                "price": float(price),
                "discount_price": float(discount_price) if discount_price else None,
                "image": final_image_url,
                "image_public_id": final_public_id
            })

        return redirect(url_for("add_product"))

    variant_of = request.args.get("variant_of", "").strip()
    prefill_variant = None
    if variant_of:
        try:
            parent_prod = products.find_one({"_id": ObjectId(variant_of)})
            if parent_prod:
                prefill_variant = {
                    "id": str(parent_prod["_id"]),
                    "name": parent_prod.get("name", ""),
                    "category": parent_prod.get("category", ""),
                    "brand": parent_prod.get("brand", ""),
                    "description": parent_prod.get("description", ""),
                    "image": parent_prod.get("image", ""),
                    "image_public_id": parent_prod.get("image_public_id", ""),
                }
        except Exception:
            pass

    products_list = list(products.find().sort("_id", -1))
    return render_template(
        "add_product.html",
        products=products_list,
        categories=categories_list,
        prefill_variant=prefill_variant
    )

@app.route("/admin/delete-product/<product_id>", methods=["POST"])
def delete_product(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1"

    if not session.get("admin_logged_in"):
        if is_ajax:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        return redirect(url_for("admin_login"))

    try:
        product = products.find_one({"_id": ObjectId(product_id)})

        if product:
            public_id = product.get("image_public_id")
            if public_id:
                try:
                    cloudinary.uploader.destroy(public_id, invalidate=True)
                except Exception:
                    pass

        products.delete_one({"_id": ObjectId(product_id)})

        if is_ajax:
            return jsonify({
                "success": True, 
                "product_id": str(product_id),
                "message": "Product removed from catalog successfully."
            })
    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": str(e)}), 500

    referrer = request.referrer or ""
    if "inventory" in referrer:
        return redirect(url_for("admin_inventory"))
    return redirect(url_for("add_product"))


@app.route("/admin/edit-product/<product_id>", methods=["GET", "POST"])
def edit_product(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1"

    if not session.get("admin_logged_in"):
        if is_ajax:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        return redirect(url_for("admin_login"))

    product = products.find_one({"_id": ObjectId(product_id)})
    if not product:
        if is_ajax:
            return jsonify({"success": False, "message": "Product not found"}), 404
        return redirect(url_for("add_product"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        price = request.form.get("price", "").strip()
        discount_price = request.form.get("discount_price", "").strip()
        unit = request.form.get("unit", "").strip()
        category = request.form.get("category", "").strip()
        other_category = request.form.get("other_category", "").strip()
        brand = request.form.get("brand", "").strip()
        description = request.form.get("description", "").strip()

        if not all([name, price, unit, category]):
            if is_ajax:
                return jsonify({"success": False, "message": "All required fields (Name, Price, Unit, Category) must be provided."}), 400
            return render_template(
                "edit_product.html",
                error="All fields are required.",
                product=product,
                categories=get_cached_categories()
            )

        try:
            price_val = float(price)
            if price_val <= 0:
                raise ValueError()
        except ValueError:
            if is_ajax:
                return jsonify({"success": False, "message": "Listing price must be a valid positive number."}), 400
            return render_template(
                "edit_product.html",
                error="Listing price must be a valid positive number.",
                product=product,
                categories=get_cached_categories()
            )

        if category == "Other":
            if not other_category:
                if is_ajax:
                    return jsonify({"success": False, "message": "Please enter a custom category name."}), 400
                return render_template(
                    "edit_product.html",
                    error="Please enter a custom category name.",
                    product=product,
                    categories=get_cached_categories()
                )
            is_valid_cat, cleaned_cat, cat_err = validate_category_name(other_category)
            if not is_valid_cat:
                if is_ajax:
                    return jsonify({"success": False, "message": f"Invalid category: {cat_err}"}), 400
                return render_template(
                    "edit_product.html",
                    error=f"Invalid category: {cat_err}",
                    product=product,
                    categories=get_cached_categories()
                )
            category = cleaned_cat
            try:
                norm_c = normalize_category_name(category)
                categories_col.update_one(
                    {"normalized_name": norm_c},
                    {"$set": {"name": category, "normalized_name": norm_c, "updated_at": datetime.datetime.now(IST)}},
                    upsert=True
                )
                cache.delete_memoized(get_cached_categories)
            except Exception:
                pass

        # base update document
        update_doc = {
            "name": name,
            "price": price_val,
            "unit": unit,
            "category": category,
        }

        if brand:
            update_doc["brand"] = brand
        if description:
            update_doc["description"] = description

        # optional stock toggle
        if "out_of_stock" in request.form:
            oos_val = request.form.get("out_of_stock")
            update_doc["out_of_stock"] = (oos_val in ("1", "true", "on"))

        unset_fields = {}
        if discount_price:
            try:
                disc_val = float(discount_price)
                if disc_val <= 0:
                    raise ValueError()
                if disc_val > price_val:
                    if is_ajax:
                        return jsonify({"success": False, "message": f"Selling price (₹{disc_val:.2f}) cannot exceed MRP (₹{price_val:.2f})."}), 400
                    return render_template(
                        "edit_product.html",
                        error=f"Selling price (₹{disc_val:.2f}) cannot exceed MRP (₹{price_val:.2f}).",
                        product=product,
                        categories=get_cached_categories()
                    )
                update_doc["discount_price"] = disc_val
            except ValueError:
                if is_ajax:
                    return jsonify({"success": False, "message": "Please enter a valid discounted price."}), 400
                return render_template(
                    "edit_product.html",
                    error="Please enter a valid discounted price.",
                    product=product,
                    categories=get_cached_categories()
                )
        else:
            # If blank, remove discount price
            unset_fields["discount_price"] = ""

        # image update (optional)
        if "image" in request.files:
            file = request.files["image"]
            if file and file.filename:
                # delete old image if exists
                old_public_id = product.get("image_public_id")
                if old_public_id:
                    try:
                        cloudinary.uploader.destroy(old_public_id, invalidate=True)
                    except Exception:
                        pass
                
                upload_result = cloudinary.uploader.upload(file, folder="products")
                update_doc["image"] = upload_result["secure_url"]
                update_doc["image_public_id"] = upload_result["public_id"]

        # Proper MongoDB update payload
        update_payload = {"$set": update_doc}
        if unset_fields:
            update_payload["$unset"] = unset_fields

        products.update_one(
            {"_id": ObjectId(product_id)}, 
            update_payload
        )

        try:
            cache.delete_memoized(get_cached_categories)
        except Exception:
            pass

        if is_ajax:
            updated_p = products.find_one({"_id": ObjectId(product_id)})
            return jsonify({
                "success": True,
                "message": f"'{name}' updated successfully!",
                "product": {
                    "_id": str(updated_p["_id"]),
                    "name": updated_p.get("name", ""),
                    "price": float(updated_p.get("price", 0)),
                    "discount_price": float(updated_p.get("discount_price")) if updated_p.get("discount_price") else None,
                    "unit": updated_p.get("unit", ""),
                    "category": updated_p.get("category", ""),
                    "brand": updated_p.get("brand", ""),
                    "description": updated_p.get("description", ""),
                    "out_of_stock": bool(updated_p.get("out_of_stock", False)),
                    "image": updated_p.get("image", "")
                }
            })

        referrer = request.referrer or ""
        if "add-product" in referrer:
            return redirect(url_for("add_product"))
        return redirect(url_for("admin_inventory"))

    return render_template("edit_product.html", product=product, categories=get_cached_categories())

# ================================
# ADMIN: ADD VENDOR/DELIVERY USER
# ================================

@app.route("/admin/add-user", methods=["GET", "POST"])
def add_user():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        role = request.form.get("role", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        vendor_type = request.form.get("vendor_type", "").strip()
        vendor_type_other = request.form.get("vendor_type_other", "").strip()
        
        shop_name = request.form.get("shop_name", "").strip()
        shop_image = request.form.get("shop_image", "").strip()
        serviceable_societies = request.form.getlist("serviceable_societies")

        # Phone validation (10 digit Indian mobile) -> normalize to +91
        raw_phone = request.form.get("phone", "").strip()
        phone = None
        phone_error = None
        if raw_phone:
            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) == 10 and digits[0] in "6789":
                phone = "+91" + digits
            elif len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
                phone = "+" + digits
            elif len(digits) == 10:
                phone = "+91" + digits
            elif len(digits) == 12 and digits.startswith("91"):
                phone = "+" + digits
            else:
                phone_error = "Please enter a valid 10-digit mobile number (e.g. 9876543210)"
        else:
            phone_error = "Mobile number is required for all personnel (Vendors & Delivery Riders)"

        # Lat / Lng from form (optional, backwards-compatible)
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()
        try:
            lat = float(raw_lat) if raw_lat else None
            lng = float(raw_lng) if raw_lng else None
        except ValueError:
            lat = None
            lng = None

        # Comprehensive validation checks
        error = None
        if role not in ["vendor", "delivery"]:
            error = "Please choose a valid role (Store Vendor or Logistics Partner)"
        elif not name or len(name) < 2:
            error = "Full Legal Name is required (minimum 2 characters)"
        elif not email or "@" not in email or "." not in email:
            error = "A valid official email address is required"
        elif phone_error:
            error = phone_error
        elif not password or len(password) < 6:
            error = "Password must be at least 6 characters long"
        elif role == "vendor" and not shop_name:
            error = "Shop name is required for Store Vendor accounts"
        elif role == "vendor" and not (vendor_type_other if vendor_type == "other" else vendor_type):
            error = "Please select a store classification type for the vendor"

        # Duplicate checks in database
        if not error:
            existing_email = users.find_one({"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}})
            if existing_email:
                error = f"An account with email '{email}' already exists in the system."
        
        if not error and phone:
            existing_phone = users.find_one({"phone": phone})
            if existing_phone:
                error = f"An account with mobile number '{phone}' already exists in the system."

        if error:
            vendors = list(users.find({"role": "vendor"}).sort("_id", -1))
            deliveries = list(users.find({"role": "delivery"}).sort("_id", -1))
            all_societies = list(societies.find())
            return render_template(
                "add_user.html",
                error=error,
                form_data=request.form,
                vendors=vendors,
                deliveries=deliveries,
                all_societies=all_societies,
            )

        # Final vendor_type resolution
        final_vendor_type = None
        if role == "vendor":
            if vendor_type == "other":
                final_vendor_type = vendor_type_other.strip() or "General Store"
            else:
                final_vendor_type = vendor_type or "General Store"

        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        doc = {
            "name": name,
            "email": email,
            "phone": phone,
            "password": pw_hash,
            "role": role,
            "created_at": datetime.now(),
        }

        if final_vendor_type:
            doc["vendor_type"] = final_vendor_type

        # Optional coords field
        if lat is not None and lng is not None:
            doc["coords"] = {"lat": lat, "lng": lng}

        if role == "vendor":
            doc["shop_name"] = shop_name
            doc["shop_image"] = shop_image
            doc["serviceable_societies"] = serviceable_societies

        users.insert_one(doc)

        vendors = list(users.find({"role": "vendor"}).sort("_id", -1))
        deliveries = list(users.find({"role": "delivery"}).sort("_id", -1))
        all_societies = list(societies.find())
        return render_template(
            "add_user.html",
            success=f"Successfully onboarded {role.capitalize()} '{name}' ({phone})!",
            vendors=vendors,
            deliveries=deliveries,
            all_societies=all_societies,
        )

    # GET: show form + existing users (latest first)
    vendors = list(users.find({"role": "vendor"}).sort("_id", -1))
    deliveries = list(users.find({"role": "delivery"}).sort("_id", -1))
    all_societies = list(societies.find())
    return render_template("add_user.html", vendors=vendors, deliveries=deliveries, all_societies=all_societies)


@app.route("/admin/edit-user/<user_id>", methods=["GET", "POST"])
def edit_user(user_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        user = users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = None

    if not user:
        return redirect(url_for("add_user"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "").strip()
        vendor_type = request.form.get("vendor_type", "").strip()
        vendor_type_other = request.form.get("vendor_type_other", "").strip()

        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # Phone edit (10-digit -> +91)
        raw_phone = request.form.get("phone", "").strip()
        phone = None
        if raw_phone:
            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) == 10:
                phone = "+91" + digits
            elif digits.startswith("91") and len(digits) == 12:
                phone = "+" + digits
            else:
                error = "Please enter a valid 10-digit mobile number"
                all_societies = list(societies.find())
                return render_template("edit_user.html", error=error, user=user, all_societies=all_societies)

        # lat / lng from edit form
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()
        try:
            lat = float(raw_lat) if raw_lat else None
            lng = float(raw_lng) if raw_lng else None
        except ValueError:
            lat = None
            lng = None
            
        shop_name = request.form.get("shop_name", "").strip()
        shop_image = request.form.get("shop_image", "").strip()
        serviceable_societies = request.form.getlist("serviceable_societies")

        # Comprehensive validations
        error = None
        if role not in ["vendor", "delivery"]:
            error = "Please select a valid role (Store Vendor or Logistics Partner)."
        elif not name or len(name) < 2:
            error = "Full Legal Name is required (minimum 2 characters)."
        elif not email or "@" not in email or "." not in email:
            error = "A valid official email address is required."
        elif not (phone or user.get("phone")):
            error = "A valid 10-digit mobile phone number is required."
        elif new_password and new_password != confirm_password:
            error = "New password and confirm password do not match."
        elif new_password and len(new_password) < 6:
            error = "New password must be at least 6 characters long."
        elif role == "vendor" and not shop_name:
            error = "Shop name is required for Store Vendor accounts."

        # Check duplicate email across other users
        if not error:
            existing_email = users.find_one({
                "_id": {"$ne": ObjectId(user_id)},
                "email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}
            })
            if existing_email:
                error = f"Another user account with email '{email}' already exists."

        # Check duplicate phone across other users
        effective_phone = phone or user.get("phone")
        if not error and effective_phone:
            existing_phone = users.find_one({
                "_id": {"$ne": ObjectId(user_id)},
                "phone": effective_phone
            })
            if existing_phone:
                error = f"Another user account with phone '{effective_phone}' already exists."

        if error:
            temp_user = dict(user)
            temp_user["name"] = name
            temp_user["email"] = email
            temp_user["role"] = role
            temp_user["phone"] = effective_phone
            temp_user["shop_name"] = shop_name
            temp_user["shop_image"] = shop_image
            temp_user["vendor_type"] = vendor_type_other if vendor_type == "other" else vendor_type
            temp_user["serviceable_societies"] = serviceable_societies
            all_societies = list(societies.find())
            return render_template("edit_user.html", error=error, user=temp_user, all_societies=all_societies)

        final_vendor_type = None
        if role == "vendor":
            if vendor_type == "other":
                final_vendor_type = vendor_type_other.strip() or "General Store"
            else:
                final_vendor_type = vendor_type or "General Store"

        update_doc = {
            "name": name,
            "email": email,
            "role": role,
        }

        if final_vendor_type:
            update_doc["vendor_type"] = final_vendor_type
        else:
            update_doc["vendor_type"] = None

        # Phone update logic: support both vendor and delivery
        if phone:
            update_doc["phone"] = phone
        elif user.get("phone"):
            update_doc["phone"] = user.get("phone")

        # coords update (set or clear)
        if lat is not None and lng is not None:
            update_doc["coords"] = {"lat": lat, "lng": lng}
        else:
            update_doc["coords"] = None

        # Password update agar diya ho
        if new_password:
            pw_hash = bcrypt.generate_password_hash(new_password).decode("utf-8")
            update_doc["password"] = pw_hash

        if role == "vendor":
            update_doc["shop_name"] = shop_name
            update_doc["shop_image"] = shop_image
            update_doc["serviceable_societies"] = serviceable_societies

        users.update_one({"_id": ObjectId(user_id)}, {"$set": update_doc})

        return redirect(url_for("add_user"))

    # GET
    all_societies = list(societies.find())
    return render_template("edit_user.html", user=user, all_societies=all_societies)


@app.route("/admin/delete-user/<user_id>", methods=["POST"])
def delete_user(user_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        users.delete_one({"_id": ObjectId(user_id)})
    except Exception:
        pass

    return redirect(url_for("add_user"))

# ================================
# VENDOR PORTAL (Separate)
# ================================

@app.route("/vendor/login", methods=["GET", "POST"])
def vendor_login():
    if request.method == "GET":
        # Check if already authenticated via dedicated vendor cookie or active session
        vendor_cookie = request.cookies.get(VENDOR_COOKIE_NAME)
        if vendor_cookie:
            vdata = verify_vendor_token(vendor_cookie)
            if vdata and vdata.get("vendor_id"):
                try:
                    v_doc = users.find_one({"_id": ObjectId(vdata["vendor_id"]), "role": "vendor"})
                    if v_doc:
                        session.permanent = True
                        session["user_role"] = "vendor"
                        session["user_email"] = v_doc.get("email")
                        session["portal_user_id"] = str(v_doc.get("_id"))
                        session["portal_user_name"] = v_doc.get("name") or v_doc.get("shop_name") or v_doc.get("email")
                        return redirect(url_for("vendor_dashboard"))
                except Exception:
                    pass

        if session.get("user_role") == "vendor" and session.get("portal_user_id"):
            return redirect(url_for("vendor_dashboard"))

        code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["portal_captcha_code"] = code
        return render_template("vendor_login.html", captcha_code=code)

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    c = "".join([
        request.form.get("captcha1", ""),
        request.form.get("captcha2", ""),
        request.form.get("captcha3", ""),
        request.form.get("captcha4", ""),
    ])

    stored = session.get("portal_captcha_code", "")
    if not stored or c != stored:
        code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["portal_captcha_code"] = code
        return render_template(
            "vendor_login.html",
            error="Invalid captcha. Please try again.",
            captcha_code=code,
        )

    doc = users.find_one({
        "email": email,
        "role": "vendor",
    })

    stored_hash = doc.get("password") if doc else None

    if stored_hash and bcrypt.check_password_hash(stored_hash, password):
        session.permanent = True
        session["user_role"] = "vendor"
        session["user_email"] = doc.get("email")
        session["portal_user_id"] = str(doc.get("_id"))
        session["portal_user_name"] = doc.get("name") or doc.get("shop_name") or doc.get("email")

        # Set dedicated persistent 365-day vendor cookie (like Admin)
        token = generate_vendor_token(doc.get("_id"), doc.get("email"))
        resp = redirect(url_for("vendor_dashboard"))
        resp.set_cookie(
            VENDOR_COOKIE_NAME,
            value=token,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            path="/"
        )
        return resp

    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return render_template(
        "vendor_login.html",
        error="Invalid credentials.",
        captcha_code=code,
    )

@app.route("/vendor/logout")
def vendor_logout():
    session.pop("user_role", None)
    session.pop("user_email", None)
    session.pop("portal_user_id", None)
    session.pop("portal_user_name", None)
    resp = redirect(url_for("vendor_login"))
    resp.delete_cookie(VENDOR_COOKIE_NAME, path="/")
    return resp

@app.route("/vendor/refresh-captcha", methods=["POST"])
def vendor_refresh_captcha():
    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return jsonify({"captcha_code": code})


from bson import ObjectId
import datetime
from collections import defaultdict

def _get_vendor_context(include_products=False):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")

    if role != "vendor" or not portal_user_id:
        return None, redirect(url_for("vendor_login"))

    try:
        vendor_oid = ObjectId(portal_user_id)
    except Exception:
        return None, redirect(url_for("vendor_login"))

    vendor = users.find_one({"_id": vendor_oid, "role": "vendor"})
    if not vendor:
        return None, redirect(url_for("vendor_login"))

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Jippy Partner Store"
    user_name = vendor.get("name") or shop_name

    # Serviceable societies names (Single batch query instead of N sequential roundtrips)
    soc_ids = vendor.get("serviceable_societies", []) or []
    valid_oids = []
    for sid in soc_ids:
        try:
            valid_oids.append(ObjectId(sid))
        except Exception:
            pass

    resolved_societies = []
    if valid_oids:
        try:
            soc_docs = list(societies.find({"_id": {"$in": valid_oids}}))
            soc_map = {str(s["_id"]): s for s in soc_docs}
            for sid in soc_ids:
                s_doc = soc_map.get(str(sid))
                if s_doc:
                    resolved_societies.append({
                        "id": str(sid),
                        "name": s_doc.get("name", str(sid)),
                        "city": s_doc.get("city", "")
                    })
                else:
                    resolved_societies.append({"id": str(sid), "name": str(sid), "city": ""})
        except Exception:
            resolved_societies = [{"id": str(sid), "name": str(sid), "city": ""} for sid in soc_ids]
    else:
        resolved_societies = [{"id": str(sid), "name": str(sid), "city": ""} for sid in soc_ids]

    # Orders query: items with vendor_id == vendor_oid OR order level vendor_id == vendor_oid
    all_raw_orders = list(orders.find({
        "$or": [
            {"items.vendor_id": vendor_oid},
            {"vendor_id": vendor_oid}
        ]
    }).sort("created_at", -1))

    # Current IST time
    now_ist = datetime.datetime.now(IST)
    today_date = now_ist.date()

    orders_all = []
    orders_pending = []
    orders_delivered = []
    orders_cancelled = []
    orders_active = []
    orders_incoming = []
    orders_packing = []
    orders_ready = []
    orders_out_for_delivery = []
    orders_completed_today = []
    orders_history = []

    today_sales = 0.0
    today_orders_count = 0
    today_items_count = 0
    all_time_sales = 0.0

    daily_ledger_map = defaultdict(lambda: {"total": 0.0, "orders": 0, "items_count": 0})

    for o in all_raw_orders:
        created_dt = o.get("created_at")
        created_ist = None
        if isinstance(created_dt, datetime.datetime):
            if created_dt.tzinfo is None:
                created_ist = pytz.utc.localize(created_dt).astimezone(IST)
            else:
                created_ist = created_dt.astimezone(IST)
        elif isinstance(created_dt, str):
            try:
                created_ist = datetime.datetime.fromisoformat(created_dt)
                if created_ist.tzinfo is None:
                    created_ist = pytz.utc.localize(created_ist).astimezone(IST)
                else:
                    created_ist = created_ist.astimezone(IST)
            except Exception:
                created_ist = now_ist
        else:
            created_ist = now_ist

        order_day = created_ist.date() if created_ist else today_date

        vendor_items = []
        vendor_order_total = 0.0
        vendor_order_qty = 0

        for it in o.get("items", []):
            it_vid = it.get("vendor_id")
            if (it_vid and str(it_vid) == str(vendor_oid)) or (not it_vid and str(o.get("vendor_id")) == str(vendor_oid)):
                vendor_items.append(it)
                qty = int(it.get("qty", 1) or 1)
                price = float(it.get("price", 0) or 0)
                vendor_order_total += (price * qty)
                vendor_order_qty += qty

        o["vendor_items"] = vendor_items
        o["vendor_items_count"] = vendor_order_qty
        o["vendor_items_total"] = vendor_order_total
        o["created_ist"] = created_ist

        raw_cust_notes = str(o.get("notes") or o.get("order_notes") or "").strip()
        clean_note_lower = raw_cust_notes.lower()
        is_meaningful_note = bool(raw_cust_notes and clean_note_lower not in ['none', 'nil', 'n/a', 'no', '-', 'null', 'no notes', 'no note'] and not _is_noise_note_text(raw_cust_notes))
        o["customer_note"] = raw_cust_notes
        o["has_meaningful_note"] = is_meaningful_note

        raw_phone = str(o.get("user_phone") or "")
        clean_phone = re.sub(r'[^0-9]', '', raw_phone)
        if clean_phone.startswith('0') and len(clean_phone) == 11:
            clean_phone = clean_phone[1:]
        if clean_phone.startswith('91') and len(clean_phone) == 12:
            wa_phone = clean_phone
        elif len(clean_phone) == 10:
            wa_phone = '91' + clean_phone
        else:
            wa_phone = clean_phone
        o["wa_phone"] = wa_phone

        delivery_fee_val = float(o.get("delivery_fee") or 0.0)
        handling_fee_val = float(o.get("handling_fee") or 0.0)
        platform_fee_val = float(o.get("platform_fee") or 0.0)
        surge_val = float(o.get("surge_charge") or 0.0)
        disc_val = float(o.get("discount") or 0.0)
        o["calculated_grand_total"] = float(o.get("total") or o.get("total_amount") or max(0.0, vendor_order_total + delivery_fee_val + handling_fee_val + platform_fee_val + surge_val - disc_val))

        diff_sec = max(0, int((now_ist - created_ist).total_seconds())) if created_ist else 0
        if diff_sec < 60:
            o["time_ago"] = "Just now"
        elif diff_sec < 3600:
            o["time_ago"] = f"{diff_sec // 60}m ago"
        elif diff_sec < 86400:
            o["time_ago"] = f"{diff_sec // 3600}h ago"
        else:
            o["time_ago"] = created_ist.strftime("%d %b")

        vstatus = (o.get("vendor_status") or o.get("status") or "pending").lower()
        o["effective_status"] = vstatus
        o["notes_pending_pricing"] = bool(is_meaningful_note and not o.get("notes_finalized") and vstatus not in ["delivered", "cancelled", "canceled"])

        # 4 Core Categories: All, Pending, Delivered, Cancelled
        orders_all.append(o)
        if vstatus == "delivered":
            orders_delivered.append(o)
            if order_day == today_date:
                orders_completed_today.append(o)
            orders_history.append(o)

            # Sales and ledger strictly reflect delivered orders (proper full total including all charges)
            order_full_val = float(o.get("calculated_grand_total") or o.get("total") or o.get("total_amount") or vendor_order_total)
            all_time_sales += order_full_val
            daily_ledger_map[order_day]["total"] += order_full_val
            daily_ledger_map[order_day]["orders"] += 1
            daily_ledger_map[order_day]["items_count"] += vendor_order_qty

            if order_day == today_date:
                today_sales += order_full_val
                today_orders_count += 1
                today_items_count += vendor_order_qty
        elif vstatus in ["cancelled", "canceled"]:
            orders_cancelled.append(o)
            orders_history.append(o)
        else:
            orders_pending.append(o)
            orders_active.append(o)
            orders_incoming.append(o)

    daily_ledger = []
    for d, stats in sorted(daily_ledger_map.items(), key=lambda x: x[0], reverse=True):
        daily_ledger.append({
            "date": d,
            "date_formatted": d.strftime("%d %B %Y"),
            "is_today": (d == today_date),
            "orders": stats["orders"],
            "items_count": stats["items_count"],
            "total": stats["total"],
            "status": "Settled" if d < today_date else "Processing"
        })

    # Catalog & Inventory (Only query full product list when required by /vendor/inventory)
    catalog_products = []
    in_stock_count = 0
    oos_count = 0

    if include_products:
        raw_products = list(products.find().sort("name", 1))
        for p in raw_products:
            p_id_str = str(p["_id"])
            oos_vendors = [str(vid) for vid in p.get("out_of_stock_vendors", [])]
            is_oos = bool(p.get("out_of_stock", False) or (str(vendor_oid) in oos_vendors))

            if is_oos:
                oos_count += 1
            else:
                in_stock_count += 1

            catalog_products.append({
                "id": p_id_str,
                "name": p.get("name", "Product"),
                "category": p.get("category", "General"),
                "unit": p.get("unit", "1 pc"),
                "price": float(p.get("price", 0)),
                "discount_price": float(p.get("discount_price") or p.get("price", 0)),
                "image": p.get("image", ""),
                "brand": p.get("brand", ""),
                "is_oos": is_oos,
            })
    else:
        try:
            total_prods = products.count_documents({})
            oos_count = products.count_documents({
                "$or": [
                    {"out_of_stock": True},
                    {"out_of_stock_vendors": vendor_oid},
                    {"out_of_stock_vendors": str(vendor_oid)}
                ]
            })
            in_stock_count = max(0, total_prods - oos_count)
        except Exception:
            in_stock_count = 0
            oos_count = 0

    try:
        categories_list = get_cached_categories()
    except Exception:
        categories_list = []

    is_online = vendor.get("is_online")
    if is_online is None:
        is_online = True
    store_notice = vendor.get("store_notice", "")

    # Khata (Store Credit) Stats
    khata_pending_count = 0
    khata_active_count = 0
    khata_outstanding_total = 0.0
    try:
        khata_col = db["vendor_khata_users"]
        khata_pending_count = khata_col.count_documents({"vendor_id": vendor_oid, "status": "PENDING"})
        khata_active_count = khata_col.count_documents({"vendor_id": vendor_oid, "status": "ACTIVE"})
        agg_res = list(khata_col.aggregate([
            {"$match": {"vendor_id": vendor_oid, "status": "ACTIVE"}},
            {"$group": {"_id": None, "total": {"$sum": "$current_balance"}}}
        ]))
        if agg_res and "total" in agg_res[0]:
            khata_outstanding_total = round(float(agg_res[0]["total"]), 2)
    except Exception:
        pass

    ctx = {
        "vendor": vendor,
        "shop_name": shop_name,
        "user_name": user_name,
        "is_online": is_online,
        "store_notice": store_notice,
        "societies": resolved_societies,
        "orders_all": orders_all,
        "orders_pending": orders_pending,
        "orders_delivered": orders_delivered,
        "orders_cancelled": orders_cancelled,
        "orders_active": orders_pending,
        "orders_incoming": orders_pending,
        "orders_packing": orders_packing,
        "orders_ready": orders_ready,
        "orders_out_for_delivery": orders_out_for_delivery,
        "orders_completed_today": orders_completed_today,
        "orders_history": orders_history,
        "today_sales": today_sales,
        "today_orders_count": today_orders_count,
        "today_items_count": today_items_count,
        "all_count": len(orders_all),
        "pending_count": len(orders_pending),
        "delivered_count": len(orders_delivered),
        "cancelled_count": len(orders_cancelled),
        "packing_count": 0,
        "ready_count": 0,
        "out_for_delivery_count": 0,
        "all_time_sales": all_time_sales,
        "daily_ledger": daily_ledger,
        "catalog_products": catalog_products,
        "total_products_count": len(catalog_products),
        "in_stock_count": in_stock_count,
        "oos_count": oos_count,
        "khata_pending_count": khata_pending_count,
        "khata_active_count": khata_active_count,
        "khata_outstanding_total": khata_outstanding_total,
        "categories": categories_list,
        "now_ist": now_ist,
    }
    return ctx, None


@app.route("/vendor/dashboard")
def vendor_dashboard():
    ctx, err_redirect = _get_vendor_context()
    if err_redirect:
        return err_redirect
    return render_template("vendor_dashboard.html", **ctx)


@app.route("/vendor/orders")
def vendor_orders():
    ctx, err_redirect = _get_vendor_context()
    if err_redirect:
        return err_redirect
    resp = make_response(render_template("vendor_orders.html", **ctx))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _is_noise_note_text(text):
    if not text:
        return True
    clean = str(text).strip().strip('"\'').strip(':').strip('-').strip()
    return bool(re.match(
        r'^(?:special\s+request\s+items?|special\s+requests?|special\s+notes?|special\s+items?|notes?|instructions?|requests?|custom\s+items?|custom\s+request|order\s+notes?)$',
        clean,
        flags=re.IGNORECASE
    ))


def _parse_note_items(raw_note):
    if not raw_note:
        return []
    text = str(raw_note).strip()
    if not text:
        return []

    # Strip prefixes like "Special Request Items:", "Special Note:", "Special Request:", "Note:"
    text = re.sub(
        r'^(?:special\s+request\s+items?|special\s+requests?|special\s+notes?|special\s+instructions?|notes?|instructions?|requests?|custom\s+items?)\s*[:\-]\s*',
        '',
        text,
        flags=re.IGNORECASE
    ).strip()

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        raw_candidates = lines
    else:
        # Check for numbering patterns like "1. item 2. item" or "1) item 2) item"
        numbered = re.split(r'(?:^|\s+)(?:\d+[\.\)]|\-|\*)\s+', text)
        numbered = [c.strip() for c in numbered if c.strip()]
        if len(numbered) > 1:
            raw_candidates = numbered
        elif ',' in text:
            raw_candidates = [c.strip() for c in text.split(',') if c.strip()]
        elif ';' in text:
            raw_candidates = [c.strip() for c in text.split(';') if c.strip()]
        elif ' + ' in text:
            raw_candidates = [c.strip() for c in text.split(' + ') if c.strip()]
        elif ' and ' in text.lower():
            raw_candidates = [c.strip() for c in re.split(r'\s+and\s+', text, flags=re.IGNORECASE) if c.strip()]
        elif ' aur ' in text.lower():
            raw_candidates = [c.strip() for c in re.split(r'\s+aur\s+', text, flags=re.IGNORECASE) if c.strip()]
        else:
            raw_candidates = [text]

    items = []
    for cand in raw_candidates:
        clean = re.sub(r'^(?:\d+[\.\)]|\-|\*)\s*', '', cand).strip().strip('"\'')
        if clean and not _is_noise_note_text(clean):
            items.append(clean)
    return items


@app.route("/vendor/order/<order_id>")
def vendor_order_detail(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return redirect(url_for("vendor_login"))

    try:
        vendor_oid = ObjectId(portal_user_id)
    except Exception:
        return redirect(url_for("vendor_login"))

    vendor = users.find_one({"_id": vendor_oid, "role": "vendor"})
    if not vendor:
        return redirect(url_for("vendor_login"))

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Jippy Partner Store"
    user_name = vendor.get("name") or shop_name
    is_online = bool(vendor.get("is_online", True))

    order = orders.find_one({"order_id": order_id})
    if not order:
        flash("Order not found", "error")
        return redirect(url_for("vendor_orders"))

    # Lightweight count query for pending orders badge
    try:
        pending_count = orders.count_documents({
            "$or": [{"items.vendor_id": vendor_oid}, {"vendor_id": vendor_oid}],
            "vendor_status": {"$nin": ["delivered", "cancelled", "canceled"]},
            "status": {"$nin": ["delivered", "cancelled", "canceled"]}
        })
    except Exception:
        pending_count = 0

    vendor_items = []
    vendor_total = 0.0
    items_mrp_total = 0.0
    total_qty = 0
    for it in order.get("items", []):
        it_name = str(it.get("name") or "").strip()
        if _is_noise_note_text(it_name):
            continue
        it_vid = it.get("vendor_id")
        if (it_vid and str(it_vid) == str(vendor_oid)) or (not it_vid and str(order.get("vendor_id")) == str(vendor_oid)):
            vendor_items.append(it)
            qty = int(it.get("qty", 1) or 1)
            price = float(it.get("price", 0) or 0)
            mrp = float(it.get("mrp") or price)
            vendor_total += (price * qty)
            items_mrp_total += (mrp * qty)
            total_qty += qty

    product_savings = max(0.0, items_mrp_total - vendor_total)
    delivery_fee = float(order.get("delivery_fee") or 0.0)
    handling_fee = float(order.get("handling_fee") or 0.0)
    platform_fee = float(order.get("platform_fee") or 0.0)
    surge_charge = float(order.get("surge_charge") or 0.0)
    coupon_discount = float(order.get("discount") or 0.0)

    order_grand_total = float(order.get("total") or order.get("total_amount") or (vendor_total + delivery_fee + handling_fee + platform_fee + surge_charge - coupon_discount))

    created_dt = order.get("created_at")
    if isinstance(created_dt, datetime.datetime):
        created_ist = pytz.utc.localize(created_dt).astimezone(IST) if created_dt.tzinfo is None else created_dt.astimezone(IST)
    elif isinstance(created_dt, str):
        try:
            created_ist = datetime.datetime.fromisoformat(created_dt).astimezone(IST)
        except Exception:
            created_ist = datetime.datetime.now(IST)
    else:
        created_ist = datetime.datetime.now(IST)

    delivered_dt = order.get("delivered_at")
    delivered_ist = None
    if isinstance(delivered_dt, datetime.datetime):
        delivered_ist = pytz.utc.localize(delivered_dt).astimezone(IST) if delivered_dt.tzinfo is None else delivered_dt.astimezone(IST)

    vstatus = (order.get("vendor_status") or order.get("status") or "pending").lower()

    raw_phone = str(order.get("user_phone") or "")
    clean_phone = re.sub(r'[^0-9]', '', raw_phone)
    if clean_phone.startswith('0') and len(clean_phone) == 11:
        clean_phone = clean_phone[1:]
    if clean_phone.startswith('91') and len(clean_phone) == 12:
        wa_phone = clean_phone
    elif len(clean_phone) == 10:
        wa_phone = "91" + clean_phone
    else:
        wa_phone = clean_phone

    # Parse individual items from customer notes
    cust_notes = str(order.get("notes") or order.get("order_notes") or "").strip()
    existing_notes_items = order.get("notes_items")
    parsed_note_items = []
    extracted_names = _parse_note_items(cust_notes)

    if existing_notes_items is not None and isinstance(existing_notes_items, list):
        for it in existing_notes_items:
            name = str(it.get("name") or "").strip()
            if name and not _is_noise_note_text(name):
                p = float(it.get("price") or 0.0)
                parsed_note_items.append({
                    "name": name,
                    "price": p,
                    "available": bool(it.get("available", p > 0))
                })
        if not parsed_note_items:
            parsed_note_items = [{"name": "", "price": 0.0, "available": False}]
    else:
        # First load: extract all items from note
        if extracted_names:
            for name in extracted_names:
                parsed_note_items.append({
                    "name": name,
                    "price": 0.0,
                    "available": False
                })
        else:
            parsed_note_items = [{"name": "", "price": 0.0, "available": False}]

    return render_template(
        "vendor_order_detail.html",
        vendor=vendor,
        shop_name=shop_name,
        user_name=user_name,
        is_online=is_online,
        order=order,
        order_id=order_id,
        items=vendor_items,
        parsed_note_items=parsed_note_items,
        vendor_total=round(vendor_total, 2),
        items_mrp_total=round(items_mrp_total, 2),
        product_savings=round(product_savings, 2),
        delivery_fee=round(delivery_fee, 2),
        handling_fee=round(handling_fee, 2),
        platform_fee=round(platform_fee, 2),
        surge_charge=round(surge_charge, 2),
        coupon_discount=round(coupon_discount, 2),
        order_grand_total=round(order_grand_total, 2),
        total_qty=total_qty,
        created_ist=created_ist,
        delivered_ist=delivered_ist,
        vstatus=vstatus,
        wa_phone=wa_phone,
        pending_count=pending_count
    )


@app.route("/vendor/inventory")
def vendor_inventory():
    ctx, err_redirect = _get_vendor_context(include_products=True)
    if err_redirect:
        return err_redirect
    return render_template("vendor_inventory.html", **ctx)


@app.route("/vendor/store")
def vendor_store():
    ctx, err_redirect = _get_vendor_context()
    if err_redirect:
        return err_redirect
    return render_template("vendor_store.html", **ctx)


@app.route("/vendor/ledger")
def vendor_ledger():
    ctx, err_redirect = _get_vendor_context()
    if err_redirect:
        return err_redirect
    return render_template("vendor_ledger.html", **ctx)


@app.route("/vendor/ledger/day/<date_str>")
def vendor_ledger_day_orders(date_str):
    ctx, err_redirect = _get_vendor_context()
    if err_redirect:
        return err_redirect

    day_orders = []
    day_total = 0.0
    day_items_count = 0
    formatted_date = date_str

    for o in ctx.get("orders_delivered", []):
        created_ist = o.get("created_ist")
        if created_ist and created_ist.strftime("%Y-%m-%d") == date_str:
            day_orders.append(o)
            order_full_val = float(o.get("calculated_grand_total") or o.get("total") or o.get("total_amount") or o.get("vendor_items_total", 0.0) or 0.0)
            day_total += order_full_val
            day_items_count += int(o.get("vendor_items_count", 0) or 0)
            formatted_date = created_ist.strftime("%d %B %Y")

    if not day_orders:
        try:
            parsed_d = datetime.date.fromisoformat(date_str)
            formatted_date = parsed_d.strftime("%d %B %Y")
        except Exception:
            formatted_date = date_str

    day_orders_count = len(day_orders)
    day_aov = round(day_total / (day_orders_count if day_orders_count > 0 else 1), 1)

    ctx["selected_date_str"] = date_str
    ctx["formatted_date"] = formatted_date
    ctx["day_orders"] = day_orders
    ctx["day_total"] = day_total
    ctx["day_items_count"] = day_items_count
    ctx["day_orders_count"] = day_orders_count
    ctx["day_aov"] = day_aov

    return render_template("vendor_ledger_day.html", **ctx)



@app.route("/vendor/api/toggle-status", methods=["POST"])
def vendor_api_toggle_status():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        vendor_oid = ObjectId(portal_user_id)
        current = users.find_one({"_id": vendor_oid})
        if not current:
            return jsonify({"success": False, "message": "Vendor not found"}), 404

        req_json = request.get_json(silent=True) or {}
        if "is_online" in req_json and isinstance(req_json["is_online"], bool):
            new_status = req_json["is_online"]
        else:
            current_status = current.get("is_online", True)
            if current_status is None:
                current_status = True
            new_status = not current_status

        users.update_one({"_id": vendor_oid}, {"$set": {"is_online": new_status}})
        session["current_shop_id"] = str(vendor_oid)

        return jsonify({
            "success": True,
            "is_online": new_status,
            "status_text": "Online" if new_status else "Offline",
            "message": "Dukaan is now ONLINE and taking orders!" if new_status else "Dukaan is now OFFLINE (Closed)."
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/api/update-notice", methods=["POST"])
def vendor_api_update_notice():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    notice = request.form.get("notice", "").strip()
    try:
        vendor_oid = ObjectId(portal_user_id)
        users.update_one({"_id": vendor_oid}, {"$set": {"store_notice": notice}})
        return jsonify({"success": True, "notice": notice, "message": "Store notice updated successfully."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/api/toggle-stock/<product_id>", methods=["POST"])
def vendor_api_toggle_stock(product_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        p_oid = ObjectId(product_id)
        p = products.find_one({"_id": p_oid})
        if not p:
            return jsonify({"success": False, "message": "Product not found"}), 404

        oos_vendors = [str(vid) for vid in p.get("out_of_stock_vendors", [])]
        is_currently_oos = bool(p.get("out_of_stock", False) or (str(portal_user_id) in oos_vendors))
        new_oos = not is_currently_oos

        if new_oos:
            if str(portal_user_id) not in oos_vendors:
                oos_vendors.append(str(portal_user_id))
        else:
            if str(portal_user_id) in oos_vendors:
                oos_vendors.remove(str(portal_user_id))
            products.update_one({"_id": p_oid}, {"$set": {"out_of_stock": False}})

        products.update_one({"_id": p_oid}, {"$set": {"out_of_stock_vendors": oos_vendors}})

        try:
            cache.clear()
        except Exception:
            pass

        return jsonify({
            "success": True,
            "product_id": product_id,
            "is_oos": new_oos,
            "status_text": "Out of Stock" if new_oos else "In Stock",
            "message": f"'{p.get('name')}' marked {'Out of Stock' if new_oos else 'In Stock'}."
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/api/update-order-status/<order_id>", methods=["POST"])
def vendor_api_update_order_status(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        vendor_oid = ObjectId(portal_user_id)
    except Exception:
        return jsonify({"success": False, "message": "Invalid vendor session"}), 401

    order_cur = orders.find_one({"order_id": order_id})
    if not order_cur:
        return jsonify({"success": False, "message": "Order not found"}), 404

    # Ownership validation: vendor must own order or have items in order
    order_vendor_id = order_cur.get("vendor_id")
    has_vendor_item = any(
        it.get("vendor_id") and str(it.get("vendor_id")) == str(vendor_oid)
        for it in order_cur.get("items", [])
    )
    if str(order_vendor_id) != str(vendor_oid) and not has_vendor_item:
        return jsonify({"success": False, "message": "Unauthorized for this order"}), 403

    new_status = request.form.get("status") or (request.json.get("status") if request.is_json else None)
    if not new_status:
        return jsonify({"success": False, "message": "Status required"}), 400

    new_status = new_status.strip().lower()
    allowed = {"received", "processing", "packed", "ready", "out_for_delivery", "delivered", "cancelled", "canceled"}
    if new_status not in allowed:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        update_fields = {
            "vendor_status": new_status,
            "status": new_status,
            f"{new_status}_at": now_utc
        }
        if new_status == "delivered":
            update_fields["delivered_at"] = now_utc
            # Finalize notes without wiping out any existing calculated price
            if not order_cur.get("notes_finalized"):
                curr_note_price = float(order_cur.get("notes_price") or 0.0)
                update_fields["notes_finalized"] = True
                update_fields["notes_available"] = bool(curr_note_price > 0)
                update_fields["notes_finalized_at"] = now_utc

            # 📱 Send Flipkart-style Delivery Confirmation WhatsApp to Customer
            if not order_cur.get("delivery_whatsapp_sent"):
                update_fields["delivery_whatsapp_sent"] = True
                try:
                    order_delivered_data = dict(order_cur)
                    order_delivered_data.update(update_fields)
                    send_order_delivery_whatsapp_async(order_delivered_data)
                except Exception as _wh_err:
                    print("[Delivery WhatsApp Dispatch Error]:", _wh_err)
        elif new_status in ["cancelled", "canceled"]:
            try:
                reverse_order_store_credit(db, order_cur, reason="Vendor Cancelled Order")
            except Exception as _rev_err:
                print("[Khata Reversal Error]:", _rev_err)

        orders.update_one({"order_id": order_id}, {"$set": update_fields})

        return jsonify({
            "success": True,
            "order_id": order_id,
            "new_status": new_status,
            "message": f"Order #{order_id[-6:].upper()} marked {new_status.replace('_', ' ').title()}"
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/api/finalize-notes-item/<order_id>", methods=["POST"])
def vendor_api_finalize_notes_item(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    order = orders.find_one({"order_id": order_id})
    if not order:
        return jsonify({"success": False, "message": "Order not found"}), 404

    try:
        vendor_oid = ObjectId(portal_user_id)
        order_vendor_id = order.get("vendor_id")
        has_vendor_item = any(
            it.get("vendor_id") and str(it.get("vendor_id")) == str(vendor_oid)
            for it in order.get("items", [])
        )
        if str(order_vendor_id) != str(vendor_oid) and not has_vendor_item:
            return jsonify({"success": False, "message": "Unauthorized for this order"}), 403

        # Prevent updating notes price once order is delivered or cancelled
        current_status = str(order.get("vendor_status") or order.get("status") or "").strip().lower()
        if current_status in ["delivered", "cancelled", "canceled"]:
            return jsonify({
                "success": False,
                "message": f"Order is already {current_status.title()}! Bill items cannot be modified after delivery."
            }), 400

        data = request.get_json(silent=True) or {}
        input_items = data.get("items")
        if not input_items or not isinstance(input_items, list):
            try:
                single_p = float(data.get("price") or 0.0)
            except (ValueError, TypeError):
                single_p = 0.0
            cust_notes = str(order.get("notes") or order.get("order_notes") or "").strip()
            single_name = str(data.get("item_name") or cust_notes or "Special Request Item").strip()
            input_items = [{"name": single_name, "price": single_p}] if single_p > 0 else []

        valid_bill_items = []
        all_notes_items = []
        total_notes_price = 0.0
        for it in input_items:
            try:
                p = float(it.get("price") or 0.0)
            except (ValueError, TypeError):
                p = 0.0
            p = max(0.0, p)
            n = str(it.get("name") or "").strip()
            # Ignore legacy / dummy "Special Request Item" or "Special Note" rows
            if _is_noise_note_text(n):
                continue
            if n:
                if len(n) > 80:
                    n = n[:77] + "..."
                if p > 0:
                    valid_bill_items.append({"name": n, "price": round(p, 2), "available": True})
                    all_notes_items.append({"name": n, "price": round(p, 2), "available": True})
                    total_notes_price += round(p, 2)
                else:
                    all_notes_items.append({"name": n, "price": 0.0, "available": False})

        # Strip any previous custom note items OR noise rows from order items
        current_items = [it for it in order.get("items", []) if not it.get("is_note_item") and not _is_noise_note_text(it.get("name"))]

        # Add all valid note items into order items
        for it in valid_bill_items:
            current_items.append({
                "name": it["name"],
                "price": it["price"],
                "mrp": it["price"],
                "qty": 1,
                "unit": "Custom Item",
                "is_note_item": True,
                "vendor_id": vendor_oid,
                "image": ""
            })

        # Recalculate subtotal
        subtotal = sum(float(it.get("price", 0) or 0) * int(it.get("qty", 1) or 1) for it in current_items)
        delivery_fee = float(order.get("delivery_fee") or 0.0)
        handling_fee = float(order.get("handling_fee") or 0.0)
        platform_fee = float(order.get("platform_fee") or 0.0)
        surge_charge = float(order.get("surge_charge") or 0.0)
        discount = float(order.get("discount") or 0.0)

        grand_total = max(0.0, subtotal + delivery_fee + handling_fee + platform_fee + surge_charge - discount)
        is_available = len(valid_bill_items) > 0
        item_names_str = ", ".join(it["name"] for it in valid_bill_items) if valid_bill_items else ""

        # Check credit limit for Store Credit orders
        is_khata = str(order.get("payment_method") or "").upper() in ["STORE_CREDIT", "UDHAR", "KHATA", "CREDIT"]
        khata_rec = None
        if is_khata:
            khata_rec, _ = _resolve_order_khata_record(db, order)

        action_mode = str(data.get("action_mode") or "").strip().upper()
        notes_cod_extra = 0.0
        credit_billed_cap = None

        if is_khata and khata_rec:
            credit_limit = float(khata_rec.get("credit_limit") or 0.0)
            if credit_limit > 0:
                current_billed = 0.0
                for e in db["vendor_khata_ledger"].find({"order_id": order_id}):
                    amt = float(e.get("amount") or 0.0)
                    if e.get("type") == "DEBIT":
                        current_billed += amt
                    elif e.get("type") == "CREDIT":
                        current_billed -= amt
                if current_billed == 0.0:
                    current_billed = float(order.get("total") or 0.0)

                diff = round(grand_total - current_billed, 2)
                curr_bal = float(khata_rec.get("current_balance") or 0.0)
                new_bal = round(curr_bal + diff, 2)

                if diff > 0 and new_bal > credit_limit:
                    exceeded_by = round(new_bal - credit_limit, 2)
                    available_limit = max(0.0, round(credit_limit - curr_bal, 2))

                    if not action_mode:
                        cust_name = khata_rec.get("customer_name") or order.get("user_name") or "Resident"
                        flat_info = khata_rec.get("full_address_display") or ""
                        return jsonify({
                            "success": False,
                            "credit_limit_exceeded": True,
                            "customer_name": cust_name,
                            "flat_info": flat_info,
                            "credit_limit": credit_limit,
                            "current_balance": curr_bal,
                            "available_limit": available_limit,
                            "diff": diff,
                            "new_balance": new_bal,
                            "exceeded_by": exceeded_by,
                            "message": f"Customer credit limit of ₹{credit_limit:,.2f} will be exceeded by ₹{exceeded_by:,.2f}."
                        }), 200

                    elif action_mode == "SPLIT_COD":
                        credit_billed_cap = round(current_billed + available_limit, 2)
                        notes_cod_extra = round(grand_total - credit_billed_cap, 2)

                    elif action_mode == "APPROVE_CREDIT":
                        notes_cod_extra = 0.0
                        credit_billed_cap = None

        update_fields = {
            "items": current_items,
            "subtotal": round(subtotal, 2),
            "total": round(grand_total, 2),
            "total_amount": round(grand_total, 2),
            "notes_available": is_available,
            "notes_price": round(total_notes_price, 2),
            "notes_items": all_notes_items,
            "notes_item_name": item_names_str,
            "notes_finalized": True,
            "notes_finalized_at": datetime.datetime.now(datetime.timezone.utc),
            "notes_cod_extra": notes_cod_extra,
            "notes_credit_billed": credit_billed_cap if credit_billed_cap is not None else grand_total,
            "notes_action_mode": action_mode or "NORMAL"
        }

        orders.update_one({"order_id": order_id}, {"$set": update_fields})
        order.update(update_fields)

        # Sync store credit ledger if order was placed via Store Credit
        try:
            if action_mode == "APPROVE_CREDIT":
                items_desc = f"Added: {item_names_str} (+₹{total_notes_price:,.2f}) [Credit Limit Override Authorized]"
            elif action_mode == "SPLIT_COD":
                items_desc = f"Added: {item_names_str} (+₹{total_notes_price:,.2f}) [Split: ₹{notes_cod_extra:,.2f} COD at Doorstep]"
            else:
                items_desc = f"Added: {item_names_str} (+₹{total_notes_price:,.2f})" if valid_bill_items else "Custom Items Removed"

            adjust_order_store_credit(
                db, order, grand_total, 
                reason=items_desc, 
                vendor_oid=vendor_oid,
                credit_billed_cap=credit_billed_cap
            )
        except Exception as _adj_err:
            print("[Khata Notes Adjustment Error]:", _adj_err)

        # 📱 Notify customer via WhatsApp about added custom items & updated bill
        if valid_bill_items and total_notes_price > 0:
            try:
                send_notes_price_updated_whatsapp_async(order, item_names_str, total_notes_price, grand_total)
            except Exception as _nt_err:
                print("[Notes WhatsApp Dispatch Error]:", _nt_err)

        # Calculate vendor's specific share
        vendor_total = 0.0
        for it in current_items:
            it_vid = it.get("vendor_id")
            if (it_vid and str(it_vid) == str(vendor_oid)) or (not it_vid and str(order.get("vendor_id")) == str(vendor_oid)):
                vendor_total += (float(it.get("price", 0) or 0) * int(it.get("qty", 1) or 1))

        if valid_bill_items:
            if notes_cod_extra > 0:
                msg = f"{len(valid_bill_items)} item(s) added (₹{credit_billed_cap:,.2f} on Khata + ₹{notes_cod_extra:,.2f} Cash at Doorstep)"
            else:
                msg = f"{len(valid_bill_items)} item(s) added to bill (Total: ₹{total_notes_price:.2f})"
        else:
            msg = "Extra item charges set to ₹0"

        return jsonify({
            "success": True,
            "is_available": is_available,
            "notes_price": round(total_notes_price, 2),
            "notes_items": all_notes_items,
            "valid_bill_items": valid_bill_items,
            "subtotal": round(subtotal, 2),
            "grand_total": round(grand_total, 2),
            "vendor_total": round(vendor_total, 2),
            "items_count": len(current_items),
            "notes_cod_extra": notes_cod_extra,
            "notes_credit_billed": credit_billed_cap if credit_billed_cap is not None else grand_total,
            "message": msg
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/api/live-poll", methods=["GET"])
def vendor_api_live_poll():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        vendor_oid = ObjectId(portal_user_id)
        vendor = users.find_one({"_id": vendor_oid, "role": "vendor"}) or users.find_one({"_id": vendor_oid})
        is_online = bool(vendor.get("is_online", True)) if vendor else True
        now_ist = datetime.datetime.now(IST)
        today_date = now_ist.date()

        # Fetch recent 60 orders so frontend can track status changes (deliveries/cancellations)
        recent_orders_raw = list(orders.find({
            "$or": [{"items.vendor_id": vendor_oid}, {"vendor_id": vendor_oid}]
        }).sort("created_at", -1).limit(60))

        # Calculate all vendor orders stats
        all_vendor_orders = list(orders.find({
            "$or": [{"items.vendor_id": vendor_oid}, {"vendor_id": vendor_oid}]
        }))

        today_sales = 0.0
        today_orders_count = 0
        pending_count = 0
        delivered_count = 0
        cancelled_count = 0

        for o in all_vendor_orders:
            vstatus_check = (o.get("vendor_status") or o.get("status") or "pending").lower()
            if vstatus_check == "delivered":
                delivered_count += 1
            elif vstatus_check in ["cancelled", "canceled"]:
                cancelled_count += 1
            else:
                pending_count += 1

            created_dt = o.get("created_at")
            if isinstance(created_dt, datetime.datetime):
                c_ist = pytz.utc.localize(created_dt).astimezone(IST) if created_dt.tzinfo is None else created_dt.astimezone(IST)
            elif isinstance(created_dt, str):
                try:
                    c_ist = datetime.datetime.fromisoformat(created_dt)
                    c_ist = pytz.utc.localize(c_ist).astimezone(IST) if c_ist.tzinfo is None else c_ist.astimezone(IST)
                except Exception:
                    c_ist = now_ist
            else:
                c_ist = now_ist

            if c_ist.date() == today_date and vstatus_check == "delivered":
                today_orders_count += 1
                delivery_fee_val = float(o.get("delivery_fee") or 0.0)
                handling_fee_val = float(o.get("handling_fee") or 0.0)
                platform_fee_val = float(o.get("platform_fee") or 0.0)
                surge_val = float(o.get("surge_charge") or 0.0)
                disc_val = float(o.get("discount") or 0.0)
                v_items_tot = sum(float(it.get("price", 0) or 0) * int(it.get("qty", 1) or 1) for it in o.get("items", [])
                                  if (it.get("vendor_id") and str(it.get("vendor_id")) == str(vendor_oid)) or (not it.get("vendor_id") and str(o.get("vendor_id")) == str(vendor_oid)))
                order_grand = float(o.get("total") or o.get("total_amount") or max(0.0, v_items_tot + delivery_fee_val + handling_fee_val + platform_fee_val + surge_val - disc_val))
                today_sales += order_grand

        serialized_orders = []
        for o in recent_orders_raw:
            created_dt = o.get("created_at")
            if isinstance(created_dt, datetime.datetime):
                c_ist = pytz.utc.localize(created_dt).astimezone(IST) if created_dt.tzinfo is None else created_dt.astimezone(IST)
            elif isinstance(created_dt, str):
                try:
                    c_ist = datetime.datetime.fromisoformat(created_dt)
                    c_ist = pytz.utc.localize(c_ist).astimezone(IST) if c_ist.tzinfo is None else c_ist.astimezone(IST)
                except Exception:
                    c_ist = now_ist
            else:
                c_ist = now_ist

            diff_sec = max(0, int((now_ist - c_ist).total_seconds()))
            if diff_sec < 60:
                time_ago = "Just now"
            elif diff_sec < 3600:
                time_ago = f"{diff_sec // 60}m ago"
            elif diff_sec < 86400:
                time_ago = f"{diff_sec // 3600}h ago"
            else:
                time_ago = c_ist.strftime("%d %b")

            vendor_items = []
            vendor_order_total = 0.0
            vendor_order_qty = 0
            for it in o.get("items", []):
                it_vid = it.get("vendor_id")
                if (it_vid and str(it_vid) == str(vendor_oid)) or (not it_vid and str(o.get("vendor_id")) == str(vendor_oid)):
                    q = int(it.get("qty", 1) or 1)
                    p = float(it.get("price", 0) or 0)
                    vendor_items.append({
                        "name": it.get("name", "Product"),
                        "unit": it.get("unit", "1 unit"),
                        "price": p,
                        "qty": q,
                        "image": it.get("image", ""),
                        "total": round(p * q, 2)
                    })
                    vendor_order_total += (p * q)
                    vendor_order_qty += q

            vstatus = (o.get("vendor_status") or o.get("status") or "pending").lower()
            addr = o.get("address", {})
            if isinstance(addr, str):
                addr_dict = {"line1": addr, "line2": "", "city": "", "pincode": ""}
            else:
                addr_dict = {
                    "line1": addr.get("line1", ""),
                    "line2": addr.get("line2", ""),
                    "city": addr.get("city", ""),
                    "pincode": addr.get("pincode", "")
                }

            user_phone = str(o.get("user_phone", "")).strip()
            clean_phone = re.sub(r'[^0-9]', '', user_phone)
            if clean_phone.startswith('0') and len(clean_phone) == 11:
                clean_phone = clean_phone[1:]
            if clean_phone.startswith('91') and len(clean_phone) == 12:
                wa_phone = clean_phone
            elif len(clean_phone) == 10:
                wa_phone = '91' + clean_phone
            else:
                wa_phone = clean_phone

            raw_cust_notes = str(o.get("notes") or o.get("order_notes") or "").strip()
            clean_note_lower = raw_cust_notes.lower()
            is_meaningful_note = bool(raw_cust_notes and clean_note_lower not in ['none', 'nil', 'n/a', 'no', '-', 'null', 'no notes', 'no note'] and not _is_noise_note_text(raw_cust_notes))

            delivery_fee_val = float(o.get("delivery_fee") or 0.0)
            handling_fee_val = float(o.get("handling_fee") or 0.0)
            platform_fee_val = float(o.get("platform_fee") or 0.0)
            surge_val = float(o.get("surge_charge") or 0.0)
            disc_val = float(o.get("discount") or 0.0)
            calc_grand = float(o.get("total") or o.get("total_amount") or max(0.0, vendor_order_total + delivery_fee_val + handling_fee_val + platform_fee_val + surge_val - disc_val))

            order_id = str(o.get("order_id", ""))
            serialized_orders.append({
                "order_id": order_id,
                "short_id": order_id[-8:].upper() if len(order_id) >= 8 else order_id.upper(),
                "status": vstatus,
                "effective_status": vstatus,
                "payment_method": o.get("payment_method") or "Cash on Delivery",
                "user_name": o.get("user_name") or "Customer",
                "user_phone": user_phone,
                "wa_phone": wa_phone,
                "address": addr_dict,
                "notes": raw_cust_notes,
                "has_meaningful_note": is_meaningful_note,
                "notes_pending_pricing": bool(is_meaningful_note and not o.get("notes_finalized") and vstatus not in ["delivered", "cancelled", "canceled"]),
                "time_ago": time_ago,
                "created_time_str": c_ist.strftime("%I:%M %p"),
                "vendor_items": vendor_items,
                "vendor_items_count": vendor_order_qty,
                "vendor_items_total": round(vendor_order_total, 2),
                "calculated_grand_total": round(calc_grand, 2),
                "is_incoming": vstatus in ["pending", "assigned", "received"],
                "is_packing": vstatus in ["processing", "packed"],
                "is_ready": vstatus == "ready",
                "is_out_for_delivery": vstatus == "out_for_delivery",
            })

        latest_order = serialized_orders[0] if serialized_orders else None
        latest_id = latest_order.get("order_id") if latest_order else ""
        incoming_count = sum(1 for o in serialized_orders if o["is_incoming"])
        packing_count = sum(1 for o in serialized_orders if o["is_packing"])
        ready_count = sum(1 for o in serialized_orders if o["is_ready"])
        out_count = sum(1 for o in serialized_orders if o["is_out_for_delivery"])

        return jsonify({
            "success": True,
            "is_online": is_online,
            "active_count": len(serialized_orders),
            "incoming_count": incoming_count,
            "packing_count": packing_count,
            "ready_count": ready_count,
            "out_count": out_count,
            "today_sales": round(today_sales, 2),
            "today_orders_count": today_orders_count,
            "pending_count": pending_count,
            "delivered_count": delivered_count,
            "cancelled_count": cancelled_count,
            "all_count": len(all_vendor_orders),
            "latest_order_id": latest_id,
            "orders": serialized_orders
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/vendor/order/<order_id>/packing-slip", methods=["GET"])
def vendor_order_packing_slip(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return redirect(url_for("vendor_login"))

    vendor_oid = ObjectId(portal_user_id)
    vendor = users.find_one({"_id": vendor_oid, "role": "vendor"})
    order = orders.find_one({"order_id": order_id})

    if not order:
        return "Order not found", 404

    vendor_items = []
    vendor_total = 0.0
    for it in order.get("items", []):
        it_vid = it.get("vendor_id")
        if (it_vid and str(it_vid) == str(vendor_oid)) or (not it_vid and str(order.get("vendor_id")) == str(vendor_oid)):
            vendor_items.append(it)
            vendor_total += float(it.get("price", 0)) * int(it.get("qty", 1))

    created_dt = order.get("created_at")
    if isinstance(created_dt, datetime.datetime):
        if created_dt.tzinfo is None:
            created_ist = pytz.utc.localize(created_dt).astimezone(IST)
        else:
            created_ist = created_dt.astimezone(IST)
    else:
        created_ist = datetime.datetime.now(IST)

    return render_template(
        "vendor_packing_slip.html",
        vendor=vendor,
        order=order,
        items=vendor_items,
        vendor_total=vendor_total,
        created_ist=created_ist
    )


# ================================
# LOGISTICS / DELIVERY PORTAL (Separate)
# ================================

@app.route("/logistics/login", methods=["GET", "POST"])
def logistics_login():
    if request.method == "GET":
        code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["portal_captcha_code"] = code
        return render_template("logistics_login.html", captcha_code=code)

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    c = "".join([
        request.form.get("captcha1", ""),
        request.form.get("captcha2", ""),
        request.form.get("captcha3", ""),
        request.form.get("captcha4", ""),
    ])

    stored = session.get("portal_captcha_code", "")
    if not stored or c != stored:
        code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["portal_captcha_code"] = code
        return render_template(
            "logistics_login.html",
            error="Invalid captcha. Please try again.",
            captcha_code=code,
        )

    doc = users.find_one({
        "email": email,
        "role": "delivery",
    })

    stored_hash = doc.get("password") if doc else None

    if stored_hash and bcrypt.check_password_hash(stored_hash, password):
        session["user_role"] = "delivery"
        session["user_email"] = doc.get("email")
        session["portal_user_id"] = str(doc.get("_id"))
        session["portal_user_name"] = doc.get("name") or doc.get("email")
        return redirect(url_for("logistics_dashboard"))

    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return render_template(
        "logistics_login.html",
        error="Invalid credentials.",
        captcha_code=code,
    )

@app.route("/logistics/logout")
def logistics_logout():
    session.pop("user_role", None)
    session.pop("user_email", None)
    session.pop("portal_user_id", None)
    session.pop("portal_user_name", None)
    return redirect(url_for("logistics_login"))

@app.route("/logistics/refresh-captcha", methods=["POST"])
def logistics_refresh_captcha():
    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return jsonify({"captcha_code": code})

@app.route("/logistics/dashboard")
def logistics_dashboard():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    user_name = session.get("portal_user_name", "Delivery")

    if role != "delivery" or not portal_user_id:
        return redirect(url_for("logistics_login"))

    delivery_oid = ObjectId(portal_user_id)
    all_delivery_orders = list(
        orders.find({"delivery_id": delivery_oid}).sort("created_at", -1)
    )

    delivery_orders_active = []
    delivery_orders_history = []

    for o in all_delivery_orders:
        dstatus = (o.get("delivery_status") or "").lower().strip()

        items_raw = o.get("items", [])
        if items_raw:
            if not isinstance(items_raw, list):
                items_raw = list(items_raw)

            o["items"] = [
                it for it in items_raw 
                if (
                    it.get("name") and
                    isinstance(it.get("price", 0), (int, float)) and
                    (float(it.get("price", 0)) or 0) > 0 and
                    isinstance(it.get("qty", 0), (int, float)) and
                    int(it.get("qty", 0)) > 0
                )
            ]

            if o['items']:
                sample_item = o['items'][0]
                print(f"   Sample keys: {list(sample_item.keys())}")
                print(f"   Sample data: {sample_item}")

        else:
            o["items"] = []

        if dstatus == "delivered":
            delivery_orders_history.append(o)
        else:
            delivery_orders_active.append(o)

    current_date = datetime.datetime.now(datetime.timezone.utc)

    return render_template(
        "logistics_dashboard.html",
        role="delivery",
        user_name=user_name,
        current_date=current_date,
        delivery_orders=delivery_orders_active,
        delivery_orders_history=delivery_orders_history,
    )


# ================================
# BACKWARD COMPATIBILITY — Old /portal/* routes redirect
# ================================

@app.route("/portal/login", methods=["GET", "POST"])
def portal_login():
    return redirect(url_for("vendor_login"))

@app.route("/portal/logout")
def portal_logout():
    role = session.get("user_role")
    if role == "delivery":
        return redirect(url_for("logistics_logout"))
    return redirect(url_for("vendor_logout"))

@app.route("/portal/dashboard")
def portal_dashboard():
    role = session.get("user_role")
    if role == "delivery":
        return redirect(url_for("logistics_dashboard"))
    elif role == "vendor":
        return redirect(url_for("vendor_dashboard"))
    return redirect(url_for("vendor_login"))

# ================================
# CUSTOMER REGISTRATION (PHONE + OPTIONAL EMAIL)
# ================================

@app.route("/register", methods=["GET"])
def register():
    return render_template("register.html")


@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


import requests

def send_green_api_message(phone, message):
    if not phone:
        return False
    url = os.getenv("WHATSAPP_API_URL", "http://3.26.1.109:3001/send-message")
    
    # Strip any non-digit characters and normalize for India
    clean_digits = re.sub(r"\D", "", str(phone))
    if len(clean_digits) == 10:
        phone_formatted = "91" + clean_digits
    elif len(clean_digits) == 12 and clean_digits.startswith("91"):
        phone_formatted = clean_digits
    else:
        phone_formatted = clean_digits

    payload = {
        "phone": phone_formatted,
        "message": message
    }
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=12)
        return response.status_code == 200
    except Exception as e:
        print("Green API Error:", e)
        return False


def format_whatsapp_order_bill(order, is_admin=False):
    """
    Flipkart-style WhatsApp order bill formatting.
    """
    user_name = order.get("user_name") or "Valued Customer"
    order_id = str(order.get("order_id", ""))
    short_id = order_id[-12:].upper() if len(order_id) >= 12 else order_id
    
    # Placed at in IST
    created_at = order.get("created_at")
    if created_at:
        if created_at.tzinfo is None:
            created_at = pytz.utc.localize(created_at)
        time_str = created_at.astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    else:
        time_str = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p")

    # Items listing
    items = order.get("items", []) or []
    items_lines = []
    for idx, it in enumerate(items, 1):
        name = it.get("name", "Item")
        unit_str = f" ({it.get('unit')})" if it.get("unit") else ""
        qty = int(it.get("qty", 1) or 1)
        price = float(it.get("price", 0) or 0)
        item_total = int(price * qty)
        items_lines.append(f"{idx}. *{name}*{unit_str}\n   Qty: {qty} × ₹{price:,.0f} = *₹{item_total:,}*")

    items_block = "\n".join(items_lines) if items_lines else "No items specified"

    # Financial details
    subtotal = float(order.get("subtotal", 0) or 0)
    handling_fee = float(order.get("handling_fee", 0) or 0)
    platform_fee = float(order.get("platform_fee", 0) or 0)
    surge_charge = float(order.get("surge_charge", 0) or 0)
    delivery_fee = float(order.get("delivery_fee", 0) or 0)
    grand_total = float(order.get("total", 0) or 0)

    fee_details = []
    fee_details.append(f"• Items Total ({len(items)} items): ₹{subtotal:,.2f}")
    if handling_fee > 0:
        fee_details.append(f"• Handling Fee: ₹{handling_fee:,.2f}")
    if platform_fee > 0:
        fee_details.append(f"• Platform Fee: ₹{platform_fee:,.2f}")
    if surge_charge > 0:
        fee_details.append(f"• Surge / Rain Fee: ₹{surge_charge:,.2f}")

    if delivery_fee == 0:
        fee_details.append("• Delivery Charges: *FREE* 🎁")
    else:
        fee_details.append(f"• Delivery Charges: ₹{delivery_fee:,.2f}")

    fees_block = "\n".join(fee_details)

    # Address
    addr = order.get("address", {}) or {}
    if isinstance(addr, dict):
        block = addr.get("block", "")
        flat = addr.get("flat_no", "")
        line1 = addr.get("line1", "")
        line2 = addr.get("line2", "")
        city = addr.get("city", "")
        pincode = addr.get("pincode", "")
        alt_phone = addr.get("alt_phone")
        
        addr_parts = []
        if block or flat:
            addr_parts.append(f"{block} - {flat}".strip(" -"))
        elif line1:
            addr_parts.append(line1)
        if line2:
            addr_parts.append(line2)
        if city or pincode:
            addr_parts.append(f"{city} - {pincode}".strip(" -"))
        address_text = "\n".join(addr_parts) if addr_parts else "Doorstep Address"
    else:
        address_text = str(addr)
        alt_phone = None

    # Special Notes / unlisted items
    notes = str(order.get("notes") or order.get("order_notes") or "").strip()
    notes_block = ""
    if notes:
        notes_block = (
            f"\n\n📝 *SPECIAL REQUEST / CUSTOM ITEMS:*\n"
            f"_{notes}_\n"
            f"*(Custom items will be verified and priced before delivery)*"
        )

    base_url = (os.getenv("BASE_URL") or "https://jippy.store").rstrip("/")
    track_url = f"{base_url}/order-confirmation?order_id={order_id}"

    if is_admin:
        header_title = "🚨 *NEW ORDER RECEIVED | JIPPY STORE* 🚨"
        header_intro = (
            f"👤 *Customer:* {user_name}\n"
            f"📱 *Phone:* {order.get('user_phone', 'N/A')}"
            + (f"\n📞 *Alt Phone:* {alt_phone}" if alt_phone else "")
        )
        footer_link = f"🔗 *Admin Orders Portal:* {base_url}/admin/orders"
    else:
        header_title = "🛍️ *ORDER CONFIRMED | JIPPY STORE* 🛍️"
        header_intro = (
            f"Dear *{user_name}*,\n"
            f"Thank you for shopping with Jippy Store! Your grocery order *#{short_id}* has been confirmed and is now being packed. ⚡ *Estimated Delivery: 10–15 mins*"
        )
        footer_link = f"🔗 *Track Live Order Status:* {track_url}"

    pay_mode_label = "Society Store Credit" if order.get("payment_method") == "STORE_CREDIT" else "Cash on Delivery (COD)"
    total_label = "TOTAL BILLED TO STORE CREDIT" if order.get("payment_method") == "STORE_CREDIT" else "TOTAL AMOUNT PAYABLE"

    msg = (
        f"{header_title}\n"
        f"═════════════════════════════\n"
        f"{header_intro}\n\n"
        f"📋 *ORDER SUMMARY:*\n"
        f"• *Order ID:* #{short_id}\n"
        f"• *Date & Time:* {time_str}\n"
        f"• *Payment Method:* {pay_mode_label}\n"
        f"═════════════════════════════\n"
        f"🛒 *ITEMS ORDERED ({len(items)}):*\n"
        f"{items_block}\n"
        f"═════════════════════════════\n"
        f"🧾 *PRICE BREAKDOWN:*\n"
        f"{fees_block}\n"
        f"─────────────────────────────\n"
        f"💵 *{total_label}: ₹{grand_total:,.2f}*\n"
        f"═════════════════════════════\n"
        f"📍 *DELIVERY ADDRESS:*\n"
        f"{address_text}"
        f"{notes_block}\n"
        f"═════════════════════════════\n"
        f"🛡️ *100% Safe & Contactless Delivery*\n"
        f"{footer_link}\n"
        f"═════════════════════════════\n"
        f"Need assistance? Reply directly to this WhatsApp message."
    )
    return msg


def send_order_whatsapp_notifications(order):
    """
    Sends WhatsApp invoice bill to customer and both Admin numbers.
    """
    try:
        # 1. Send to Customer
        customer_phone = order.get("user_phone")
        if not customer_phone or customer_phone == "guest":
            customer_phone = order.get("address", {}).get("alt_phone")
            
        if customer_phone and customer_phone != "guest":
            customer_msg = format_whatsapp_order_bill(order, is_admin=False)
            res = send_green_api_message(customer_phone, customer_msg)
            print(f"Customer WhatsApp sent to {customer_phone}: {res}")

        # 2. Send to Admin Number 1
        admin1 = os.getenv("ADMIN_NUMBER_1", "").strip()
        if admin1:
            admin_msg1 = format_whatsapp_order_bill(order, is_admin=True)
            res1 = send_green_api_message(admin1, admin_msg1)
            print(f"Admin 1 WhatsApp sent to {admin1}: {res1}")

        # 3. Send to Admin Number 2
        admin2 = os.getenv("ADMIN_NUMBER_2", "").strip()
        if admin2 and admin2 != admin1:
            admin_msg2 = format_whatsapp_order_bill(order, is_admin=True)
            res2 = send_green_api_message(admin2, admin_msg2)
            print(f"Admin 2 WhatsApp sent to {admin2}: {res2}")

    except Exception as e:
        print("Error sending WhatsApp notifications:", e)


def send_order_whatsapp_notifications_async(order):
    """
    Dispatches order confirmation WhatsApp messages in a background thread
    so the checkout API responds instantly without any latency.
    """
    threading.Thread(target=send_order_whatsapp_notifications, args=(order,), daemon=True).start()


def format_whatsapp_delivery_message(order):
    """
    Professional English WhatsApp Order Delivery notification sent to customer upon fulfillment.
    """
    user_name = order.get("user_name") or "Valued Customer"
    order_id = str(order.get("order_id", ""))
    short_id = order_id[-8:].upper() if len(order_id) >= 8 else order_id

    # Delivered at in IST
    delivered_at = order.get("delivered_at")
    if delivered_at:
        if delivered_at.tzinfo is None:
            delivered_at = pytz.utc.localize(delivered_at)
        time_str = delivered_at.astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    else:
        time_str = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p")

    # Items listing
    items = order.get("items", []) or []
    items_lines = []
    for idx, it in enumerate(items, 1):
        name = it.get("name", "Item")
        unit_str = f" ({it.get('unit')})" if it.get("unit") else ""
        qty = int(it.get("qty", 1) or 1)
        price = float(it.get("price", 0) or 0)
        item_total = int(price * qty)
        items_lines.append(f"{idx}. *{name}*{unit_str} × {qty} = *₹{item_total:,}*")

    items_block = "\n".join(items_lines) if items_lines else "Grocery Items"

    # Customized / Notes Items if any
    notes_price = float(order.get("notes_price") or 0.0)
    notes_line = ""
    if notes_price > 0:
        notes_line = f"\n• *Custom Requested Items:* ₹{notes_price:,.2f}"

    grand_total = float(order.get("total", 0) or 0)
    is_khata = str(order.get("payment_method") or "").upper() in ["STORE_CREDIT", "UDHAR", "KHATA"]
    cod_extra = float(order.get("notes_cod_extra") or 0.0)

    if is_khata and cod_extra > 0:
        credit_portion = float(order.get("notes_credit_billed") or (grand_total - cod_extra))
        pay_info = (
            f"• *Payment Method:* Society Store Credit + Cash on Delivery\n"
            f"• *Store Credit Account:* ₹{credit_portion:,.2f} billed to monthly passbook\n"
            f"• *Cash Collected at Doorstep:* ₹{cod_extra:,.2f}\n"
            f"• *View Passbook:* https://jippy.store/my-khata"
        )
    elif is_khata:
        pay_info = (
            f"• *Payment Method:* Society Store Credit\n"
            f"• *Account Status:* Billed to your monthly Store Credit passbook\n"
            f"• *View Passbook:* https://jippy.store/my-khata"
        )
    else:
        pay_info = (
            f"• *Payment Method:* Cash on Delivery (COD)\n"
            f"• *Payment Status:* Collected at Doorstep (Cash / UPI)"
        )

    # Address
    addr = order.get("address", {}) or {}
    if isinstance(addr, dict):
        block = addr.get("block", "")
        flat = addr.get("flat_no", "")
        line1 = addr.get("line1", "")
        line2 = addr.get("line2", "")
        
        addr_parts = []
        if block or flat:
            addr_parts.append(f"{block} - {flat}".strip(" -"))
        elif line1:
            addr_parts.append(line1)
        if line2:
            addr_parts.append(line2)
        address_text = "\n".join(addr_parts) if addr_parts else "Doorstep Address"
    else:
        address_text = str(addr)

    base_url = (os.getenv("BASE_URL") or "https://jippy.store").rstrip("/")
    invoice_url = f"{base_url}/order-confirmation?order_id={order_id}"

    msg = (
        f"🎉 *ORDER DELIVERED | JIPPY STORE* 📦\n"
        f"═════════════════════════════\n"
        f"Dear *{user_name}*,\n\n"
        f"Your grocery order *#{short_id}* has been successfully delivered to your doorstep. We hope everything reached you fresh and in perfect condition!\n\n"
        f"📋 *DELIVERY SUMMARY:*\n"
        f"• *Order ID:* #{short_id}\n"
        f"• *Delivery Timestamp:* {time_str}\n"
        f"{pay_info}\n"
        f"═════════════════════════════\n"
        f"🛒 *DELIVERED ITEMS ({len(items)}):*\n"
        f"{items_block}"
        f"{notes_line}\n"
        f"─────────────────────────────\n"
        f"💵 *FINAL ORDER TOTAL: ₹{grand_total:,.2f}*\n"
        f"═════════════════════════════\n"
        f"📍 *DELIVERED TO:*\n"
        f"*{address_text}*\n"
        f"═════════════════════════════\n"
        f"⭐ *DIGITAL INVOICE & FEEDBACK:*\n"
        f"View your itemized receipt and rate your experience:\n"
        f"🔗 {invoice_url}\n"
        f"═════════════════════════════\n"
        f"Need assistance? Reply directly to this WhatsApp message.\n\n"
        f"Thank you for shopping with *Jippy Store*! "
    )
    return msg


def send_order_delivery_whatsapp(order):
    """
    Sends professional Delivery Confirmation WhatsApp notification to customer.
    """
    try:
        customer_phone = order.get("user_phone")
        if not customer_phone or customer_phone == "guest":
            customer_phone = order.get("address", {}).get("alt_phone")
            
        if customer_phone and customer_phone != "guest":
            customer_msg = format_whatsapp_delivery_message(order)
            res = send_green_api_message(customer_phone, customer_msg)
            print(f"[Delivery WhatsApp sent to {customer_phone}]: {res}")
            return res
    except Exception as e:
        print("[Delivery WhatsApp Error]:", e)
        return False


def send_order_delivery_whatsapp_async(order):
    """
    Dispatches delivery WhatsApp message in background thread.
    """
    threading.Thread(target=send_order_delivery_whatsapp, args=(order,), daemon=True).start()


def send_notes_price_updated_whatsapp(order, added_items_text, added_amount, new_grand_total):
    """
    Sends WhatsApp update to customer when vendor prices custom requested items in professional English.
    """
    try:
        customer_phone = order.get("user_phone")
        if not customer_phone or customer_phone == "guest":
            customer_phone = order.get("address", {}).get("alt_phone")
        if not customer_phone or customer_phone == "guest":
            return False

        user_name = order.get("user_name") or "Valued Customer"
        order_id = str(order.get("order_id", ""))
        short_id = order_id[-8:].upper() if len(order_id) >= 8 else order_id
        is_khata = str(order.get("payment_method") or "").upper() in ["STORE_CREDIT", "UDHAR", "KHATA"]
        cod_extra = float(order.get("notes_cod_extra") or 0.0)
        credit_billed = float(order.get("notes_credit_billed") or (new_grand_total - cod_extra))

        if is_khata:
            if cod_extra > 0:
                pay_text = (
                    f"• *Payment Method:* Society Store Credit + Cash on Delivery\n"
                    f"• *Store Credit Account:* ₹{credit_billed:,.2f} billed (up to credit limit)\n"
                    f"• *Cash Payable at Doorstep:* ₹{cod_extra:,.2f} (for items exceeding credit limit)\n"
                    f"• *View Passbook:* https://jippy.store/my-khata"
                )
            else:
                pay_text = (
                    f"• *Payment Method:* Society Store Credit\n"
                    f"• *Account Status:* +₹{added_amount:,.2f} debited to your Store Credit passbook\n"
                    f"• *View Passbook:* https://jippy.store/my-khata"
                )
        else:
            pay_text = (
                f"• *Payment Method:* Cash on Delivery (COD)\n"
                f"• *Payable at Doorstep:* ₹{new_grand_total:,.2f}"
            )

        base_url = (os.getenv("BASE_URL") or "https://jippy.store").rstrip("/")
        invoice_url = f"{base_url}/order-confirmation?order_id={order_id}"

        msg = (
            f"📝 *ORDER BILL UPDATED | JIPPY STORE* 🛍️\n"
            f"═════════════════════════════\n"
            f"Dear *{user_name}*,\n\n"
            f"The custom item requested in your order *#{short_id}* has been verified and added to your bill by the store manager:\n\n"
            f"🛒 *CUSTOM ITEM DETAILS:*\n"
            f"• *{added_items_text}* (+₹{added_amount:,.2f})\n\n"
            f"🧾 *UPDATED BILL SUMMARY:*\n"
            f"• *New Order Total: ₹{new_grand_total:,.2f}*\n"
            f"{pay_text}\n"
            f"═════════════════════════════\n"
            f"🔗 *View Updated Itemized Invoice:*\n"
            f"{invoice_url}\n"
            f"═════════════════════════════\n"
            f"For any queries or modifications, please reply directly to this WhatsApp message.\n\n"
            f"Thank you for shopping with *Jippy Store*!"
        )
        return send_green_api_message(customer_phone, msg)
    except Exception as e:
        print("[Notes Update WhatsApp Error]:", e)
        return False


def send_notes_price_updated_whatsapp_async(order, added_items_text, added_amount, new_grand_total):
    threading.Thread(
        target=send_notes_price_updated_whatsapp,
        args=(order, added_items_text, added_amount, new_grand_total),
        daemon=True
    ).start()


def send_admin_push_notifications(order):
    """
    Dispatches FCM & Cloud Web Push Notifications to all registered Admin devices.
    Delivers notifications to laptops and phones even when browser tabs are closed.
    """
    try:
        short_id = str(order.get("order_id", ""))[-8:].upper()
        user_name = order.get("user_name", "Resident")
        grand_total = float(order.get("total", 0) or 0)
        items = order.get("items", [])
        item_count = len(items)
        order_id = str(order.get("order_id", ""))

        addr = order.get("address", {}) or {}
        if isinstance(addr, dict) and (addr.get("block") or addr.get("flat_no")):
            dest = f"{addr.get('block', '')} - {addr.get('flat_no', '')}".strip(" -")
        elif isinstance(addr, dict) and addr.get("line1"):
            dest = addr.get("line1")
        else:
            dest = "Doorstep"

        title = f"🛒 New Order: ₹{grand_total:,.0f} | #{short_id}"
        body = f"{user_name} placed an order with {item_count} items. Delivery to {dest}."

        # Fetch all active registered admin device tokens
        subscriptions = list(admin_push_subscriptions.find({"active": {"$ne": False}}))
        if not subscriptions:
            return

        for sub_doc in subscriptions:
            token = sub_doc.get("token")
            if token and firebase_admin._apps:
                try:
                    fcm_msg = messaging.Message(
                        notification=messaging.Notification(
                            title=title,
                            body=body,
                        ),
                        data={
                            "order_id": order_id,
                            "type": "NEW_ORDER",
                            "url": "/admin/orders"
                        },
                        token=token,
                        webpush=messaging.WebpushConfig(
                            notification=messaging.WebpushNotification(
                                title=title,
                                body=body,
                                icon="/static/logo.jpeg",
                                badge="/static/logo.jpeg",
                                require_interaction=True,
                            ),
                            fcm_options=messaging.WebpushFCMOptions(
                                link="/admin/orders"
                            )
                        )
                    )
                    messaging.send(fcm_msg)
                except Exception as fcm_err:
                    print(f"FCM push notification delivery error: {fcm_err}")
    except Exception as e:
        print("Admin push notification dispatch failed:", e)


def send_admin_push_notifications_async(order):
    threading.Thread(target=send_admin_push_notifications, args=(order,), daemon=True).start()


@app.route("/api/send-whatsapp-otp", methods=["POST"])
def send_whatsapp_otp():
    data = request.get_json()
    phone = data.get("phone", "").strip()
    action = data.get("action") # 'login' or 'register'
    
    if not phone:
        return jsonify({"success": False, "error": "Phone number is required"}), 400
        
    phone = normalize_phone(phone)
    if len(phone) < 12: # +91XXXXXXXXXX is length 13
         return jsonify({"success": False, "error": "Invalid phone number"}), 400

    if action == "login":
        if not users.find_one({"phone": phone, "role": "customer"}):
            return jsonify({"success": False, "error": "Phone number not registered. Please create an account."}), 400
    elif action == "register":
        if users.find_one({"phone": phone, "role": "customer"}):
            return jsonify({"success": False, "error": "This phone number is already registered. Please login."}), 400

    otp_code = str(random.randint(100000, 999999))
    session['whatsapp_otp'] = otp_code
    session['whatsapp_phone'] = phone
    session['whatsapp_otp_expiry'] = (datetime.datetime.now() + datetime.timedelta(minutes=5)).timestamp()
    
    msg = (
        f"🔐 *VERIFICATION CODE | JIPPY STORE*\n\n"
        f"Your one-time login verification code is: *{otp_code}*\n"
        f"(Valid for 5 minutes)\n\n"
        f"Please do not share this code with anyone for your account security.\n\n"
        f"Thank you for choosing Jippy Store! "
    )
    success = send_green_api_message(phone, msg)
    
    if success:
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "Failed to send OTP via WhatsApp. Please try again."}), 500

@app.route("/api/verify-whatsapp-otp", methods=["POST"])
def verify_whatsapp_otp():
    data = request.get_json()
    code = data.get("code", "").strip()
    action = data.get("action")
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    
    stored_otp = session.get('whatsapp_otp')
    stored_phone = session.get('whatsapp_phone')
    expiry = session.get('whatsapp_otp_expiry')
    
    if not stored_otp or not stored_phone or not expiry:
        return jsonify({"success": False, "error": "No OTP request found. Please request a new OTP."}), 400
        
    if datetime.datetime.now().timestamp() > expiry:
        return jsonify({"success": False, "error": "OTP has expired. Please request a new one."}), 400
        
    if code != stored_otp:
        return jsonify({"success": False, "error": "Invalid OTP. Please try again."}), 400
        
    # Clear OTP
    session.pop('whatsapp_otp', None)
    session.pop('whatsapp_otp_expiry', None)

    try:
        phone = stored_phone
        if action == "register":
            if not name:
                return jsonify({"success": False, "error": "Name is required for registration."}), 400
                
            doc = {
                "name": name,
                "email": email,
                "phone": phone,
                "role": "customer",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
            res = users.insert_one(doc)
            doc["_id"] = res.inserted_id
            
            session.permanent = True
            session["user_id"] = str(doc["_id"])
            session["user_phone"] = phone
            session["user_name"] = doc.get("name", "User")
            
            return jsonify({"success": True, "redirect": url_for("index")})

        elif action == "login":
            doc = users.find_one({"phone": phone, "role": "customer"})
            if not doc:
                return jsonify({"success": False, "error": "User not found."}), 400
            
            session.permanent = True
            session["user_id"] = str(doc["_id"])
            session["user_phone"] = phone
            session["user_name"] = doc.get("name", "User")
            
            return jsonify({"success": True, "redirect": url_for("index")})

    except Exception as e:
        print(f"Auth Error: {str(e)}")
        return jsonify({"success": False, "error": "An error occurred during verification."}), 500

# ================================
# CHECKOUT + CART
# ================================

@app.route("/checkout", methods=["POST"])
def checkout():
    data = request.get_json()
    cart = data.get("cart", [])
    user_phone = session.get("user_phone", "guest")
    user_name  = session.get("user_name", "Guest")
    user_id    = session.get("user_id")

    if not cart:
        return jsonify({"error": "Cart is empty"}), 400

    # ---- Store Online Validation: Prevent ordering from closed vendor ----
    shop_id = session.get("current_shop_id") or session.get("portal_user_id")
    vendor_doc = None
    if shop_id:
        try:
            vendor_doc = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            vendor_doc = None
    if not vendor_doc:
        current_society_id = session.get("current_society_id")
        if current_society_id:
            try:
                soc_oid = ObjectId(current_society_id)
                vendor_doc = users.find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(current_society_id)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                vendor_doc = users.find_one({"role": "vendor", "serviceable_societies": str(current_society_id)})
    if not vendor_doc:
        vendor_doc = users.find_one({"role": "vendor"}, sort=[("_id", -1)])

    is_closed = False
    if vendor_doc:
        val = vendor_doc.get("is_online")
        if val is False or str(val).lower() in ["false", "0", "offline"]:
            is_closed = True
    else:
        is_closed = True

    if is_closed:
        shop_title = vendor_doc.get("shop_name", "This store") if vendor_doc else "This store"
        return jsonify({
            "error": f"{shop_title} is currently closed and not accepting new orders. Please try again when the store reopens."
        }), 400

    address = data.get("address")
    if not address:
        return jsonify({"error": "Address is required"}), 400

    # ---- Standardize Address Components (BLOCK, FLAT, FLOOR, Uppercase) ----
    std_addr = standardize_address_components(
        raw_block=address.get("block", ""),
        raw_floor=address.get("floor", ""),
        raw_flat=address.get("flat_no", ""),
        raw_line1=address.get("line1", "")
    )
    address["block"] = std_addr["block"]
    address["floor"] = std_addr["floor"]
    address["flat_no"] = std_addr["flat_no"]
    address["line1"] = std_addr["line1"]

    # ---- Society Address Validation: Check delivery address is for active society ----
    current_society_id = session.get("current_society_id")
    soc_doc = None
    if current_society_id:
        try:
            soc_doc = societies.find_one({"_id": ObjectId(current_society_id)})
        except Exception:
            pass
    if not soc_doc:
        soc_doc = societies.find_one()

    if soc_doc:
        expected_pin = str(soc_doc.get("pincode", "")).strip()
        actual_pin = str(address.get("pincode", "")).strip()
        if expected_pin and actual_pin and actual_pin != expected_pin:
            return jsonify({
                "error": f"Delivery is only available in {soc_doc.get('name', 'our society')} (PIN: {expected_pin}). Please deliver to your society address."
            }), 400

    # ---- lat / lng normalize ----
    raw_lat = address.get("lat")
    raw_lng = address.get("lng")
    try:
        lat = float(raw_lat) if raw_lat not in (None, "") else None
        lng = float(raw_lng) if raw_lng not in (None, "") else None
    except (TypeError, ValueError):
        lat = None
        lng = None

    # ---- STEP 1: logged-in user ke liye address save / update ----
    if user_id:
        try:
            user_doc = users.find_one({"_id": ObjectId(user_id), "role": "customer"})
        except Exception:
            user_doc = None

        if user_doc:
            existing_addrs = user_doc.get("addresses", []) or []
            same_indexes = [
                idx for idx, a in enumerate(existing_addrs)
                if (a.get("line1") == address.get("line1"))
                and (a.get("line2") == address.get("line2"))
                and (a.get("city")  == address.get("city"))
                and (a.get("pincode") == address.get("pincode"))
            ]

            if same_indexes:
                idx = same_indexes[0]
                updates = {
                    f"addresses.{idx}.alt_phone": address.get("alt_phone"),
                    f"addresses.{idx}.location_text": address.get("location_text", ""),
                    f"addresses.{idx}.block": address.get("block", ""),
                    f"addresses.{idx}.floor": address.get("floor", ""),
                    f"addresses.{idx}.flat_no": address.get("flat_no", ""),
                }
                if lat is not None and lng is not None:
                    updates[f"addresses.{idx}.coords"] = {"lat": lat, "lng": lng}

                users.update_one({"_id": ObjectId(user_id)}, {"$set": updates})
            else:
                new_addr = {
                    "label": address.get("label") or f"Address {len(existing_addrs) + 1}",
                    "alt_phone": address.get("alt_phone"),
                    "line1": address.get("line1", ""),
                    "line2": address.get("line2", ""),
                    "block": address.get("block", ""),
                    "floor": address.get("floor", ""),
                    "flat_no": address.get("flat_no", ""),
                    "city": address.get("city", ""),
                    "pincode": address.get("pincode", ""),
                    "location_text": address.get("location_text", ""),
                    "coords": {"lat": lat, "lng": lng} if (lat is not None and lng is not None) else None,
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                }
                users.update_one({"_id": ObjectId(user_id)}, {"$push": {"addresses": new_addr}})

    # ---- STEP 2: Totals Calculation with Handling & Surge ----
    subtotal = 0
    for item in cart:
        price = float(item.get("price", 0))
        qty   = int(item.get("qty", 1))
        subtotal += price * qty

    # Fetch Financial Config from Settings
    pf_doc = settings.find_one({"key": "platform_fee"}) or {"value": 0}
    thr_doc = settings.find_one({"key": "free_delivery_threshold"}) or {"value": 49}
    fee_doc = settings.find_one({"key": "delivery_fee"}) or {"value": 10}
    hf_doc = settings.find_one({"key": "handling_fee"}) or {"value": 0} # NEW
    sc_doc = settings.find_one({"key": "surge_charge"}) or {"value": 0} # NEW

    platform_fee = int(pf_doc.get("value", 0))
    free_threshold = int(thr_doc.get("value", 49))
    base_delivery_fee = int(fee_doc.get("value", 10))
    handling_fee = int(hf_doc.get("value", 0)) # NEW
    surge_charge = int(sc_doc.get("value", 0)) # NEW

    # Apply Delivery Fee logic
    delivery_fee = 0 if subtotal >= free_threshold else base_delivery_fee

    # Final Grand Total Calculation
    grand_total = subtotal + platform_fee + delivery_fee + handling_fee + surge_charge
    order_id = generate_order_id()

    # ---- NEW: Auto-assign Vendor ----
    shop_id = session.get("current_shop_id")
    vendor_doc = None
    if shop_id:
        vendor_doc = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
    
    if not vendor_doc:
        # fallback to the first available vendor
        vendor_doc = users.find_one({"role": "vendor"})
        
    vendor_status = "pending"
    if vendor_doc:
        vendor_id = vendor_doc["_id"]
        vendor_status = "pending"
        for item in cart:
            item["vendor_id"] = vendor_id

    notes = str(data.get("notes") or data.get("order_notes") or "").strip()[:500]

    # ---- 📒 Society Store Credit (Khata) Verification & Validation ----
    raw_pm = str(data.get("payment_method") or "COD").strip().upper()
    payment_method = "STORE_CREDIT" if raw_pm in ["UDHAR", "STORE_CREDIT", "KHATA"] else "COD"
    khata_user = None
    curr_bal = 0.0

    if payment_method == "STORE_CREDIT":
        clean_p = clean_phone_10(user_phone)
        khata_user = db["vendor_khata_users"].find_one({
            "vendor_id": vendor_id,
            "$or": [
                {"customer_phone": clean_p},
                {"user_id": ObjectId(user_id) if user_id else None}
            ],
            "is_verified": True,
            "status": "ACTIVE"
        })
        if not khata_user:
            return jsonify({
                "error": "Your Store Credit (Khata) account is not verified for this store. Please choose Cash on Delivery or request store approval."
            }), 400

        credit_limit = float(khata_user.get("credit_limit") or 0.0)
        curr_bal = recalculate_khata_user_balance(db, khata_user["_id"])
        if credit_limit > 0 and (curr_bal + grand_total) > credit_limit:
            available_limit = max(0.0, credit_limit - curr_bal)
            if curr_bal < 0:
                err_msg = (
                    f"Order total (₹{grand_total:,.2f}) exceeds your available Store Credit limit of ₹{available_limit:,.2f} "
                    f"(Credit limit: ₹{credit_limit:,.0f} + Advance deposit: ₹{abs(curr_bal):,.2f}). Please pay via Pay on Delivery (COD)."
                )
            elif curr_bal > 0:
                err_msg = (
                    f"Order total (₹{grand_total:,.2f}) exceeds your available Store Credit limit of ₹{available_limit:,.2f} "
                    f"(Current due: ₹{curr_bal:,.2f} of ₹{credit_limit:,.0f} limit). Please pay via COD or settle your dues."
                )
            else:
                err_msg = (
                    f"Order total (₹{grand_total:,.2f}) exceeds your Store Credit limit of ₹{credit_limit:,.0f}. "
                    f"Please pay via Pay on Delivery (COD)."
                )
            return jsonify({"error": err_msg}), 400

    # ---- STEP 3: Order object creation ----
    order = {
        "user_id": user_id,
        "order_id": order_id,
        "user_phone": user_phone,
        "user_name": user_name,
        "items": cart,
        "notes": notes,
        "vendor_status": vendor_status,
        "subtotal": subtotal,
        "platform_fee": platform_fee,
        "delivery_fee": delivery_fee,
        "handling_fee": handling_fee, # Saved in order
        "surge_charge": surge_charge, # Saved in order
        "total": grand_total,
        "status": "pending",
        "payment_method": payment_method,
        "address": {
            "alt_phone": address.get("alt_phone"),
            "line1": address.get("line1", ""),
            "line2": address.get("line2", ""),
            "block": address.get("block", ""),
            "floor": address.get("floor", ""),
            "flat_no": address.get("flat_no", ""),
            "city": address.get("city", ""),
            "pincode": address.get("pincode", ""),
            "location_text": address.get("location_text", ""),
        },
        "coords": {"lat": lat, "lng": lng} if (lat is not None and lng is not None) else None,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }

    orders.insert_one(order)

    # 📒 Society Store Credit Ledger Debit Update
    if payment_method == "STORE_CREDIT" and khata_user:
        try:
            new_bal = curr_bal + grand_total
            db["vendor_khata_ledger"].insert_one({
                "khata_user_id": khata_user["_id"],
                "vendor_id": vendor_id,
                "user_id": ObjectId(user_id) if user_id else None,
                "customer_name": user_name,
                "customer_phone": clean_phone_10(user_phone),
                "order_id": order_id,
                "type": "DEBIT",
                "amount": round(grand_total, 2),
                "balance_before": round(curr_bal, 2),
                "balance_after": round(new_bal, 2),
                "payment_mode": "STORE_CREDIT",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            })
            # Recalculate true running balance from entire ledger history
            recalculate_khata_user_balance(db, khata_user["_id"])
            db["vendor_khata_users"].update_one(
                {"_id": khata_user["_id"]},
                {
                    "$set": {
                        "last_order_at": datetime.datetime.now(datetime.timezone.utc),
                        "updated_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                }
            )
        except Exception as _khata_err:
            print("[Khata Order Debit Error]:", _khata_err)

    # 📱 WhatsApp Notification: Send Flipkart-style Bill to User & Both Admins
    try:
        send_order_whatsapp_notifications_async(order)
    except Exception as e:
        print("WhatsApp dispatch failed:", e)

    # 🔔 Cloud Web Push & FCM Notification to Admin (Works even if browser tab closed)
    try:
        send_admin_push_notifications_async(order)
    except Exception as e:
        print("Admin push dispatch failed:", e)

    return jsonify({
        "message": "Order placed successfully",
        "order_id": order_id,
        "total": grand_total,
    })

@app.route("/api/store-status", methods=["GET"])
def api_store_status():
    """
    Real-time status sync endpoint for Cart and Shop pages.
    Returns whether the currently active shop is online or offline.
    """
    shop_id = request.args.get("shop_id") or session.get("current_shop_id") or session.get("portal_user_id")
    shop = None
    if shop_id:
        try:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            shop = None

    if not shop:
        current_society_id = session.get("current_society_id")
        if current_society_id:
            try:
                soc_oid = ObjectId(current_society_id)
                shop = users.find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(current_society_id)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                shop = users.find_one({"role": "vendor", "serviceable_societies": str(current_society_id)})
        if not shop:
            shop = users.find_one({"role": "vendor"}, sort=[("_id", -1)])

    is_online = True
    if shop:
        val = shop.get("is_online")
        if val is False or str(val).lower() in ["false", "0", "offline"]:
            is_online = False
        else:
            is_online = True
    else:
        is_online = False

    resp = jsonify({
        "success": True,
        "shop_id": str(shop.get("_id")) if shop else None,
        "shop_name": shop.get("shop_name") or shop.get("name") if shop else "Quick Store",
        "is_online": is_online,
        "is_closed": not is_online
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/cart")
def cart_page():
    is_logged_in = bool(session.get("user_id"))

    # Check store online status with society/default fallback
    shop_id = session.get("current_shop_id") or session.get("portal_user_id")
    shop = None
    if shop_id:
        try:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            shop = None

    if not shop:
        current_society_id = session.get("current_society_id")
        if current_society_id:
            try:
                soc_oid = ObjectId(current_society_id)
                shop = users.find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(current_society_id)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                shop = users.find_one({"role": "vendor", "serviceable_societies": str(current_society_id)})
        if not shop:
            shop = users.find_one({"role": "vendor"}, sort=[("_id", -1)])

    is_shop_closed = False
    if shop:
        val = shop.get("is_online")
        if val is False or str(val).lower() in ["false", "0", "offline"]:
            is_shop_closed = True

    # 1. Platform Fee
    pf_doc = settings.find_one({"key": "platform_fee"})
    platform_fee = int(pf_doc["value"]) if pf_doc and "value" in pf_doc else 0

    # 2. Free Delivery Threshold & Fee
    thr_doc = settings.find_one({"key": "free_delivery_threshold"}) or {"value": 49}
    fee_doc = settings.find_one({"key": "delivery_fee"}) or {"value": 10}
    FREE_DELIVERY_THRESHOLD = int(thr_doc.get("value", 49))
    DELIVERY_FEE = int(fee_doc.get("value", 10))

    # 3. Handling Fee (NEW)
    hf_doc = settings.find_one({"key": "handling_fee"}) or {"value": 0}
    handling_fee = int(hf_doc.get("value", 0))

    # 4. Surge Charge (NEW)
    sc_doc = settings.find_one({"key": "surge_charge"}) or {"value": 0}
    surge_charge = int(sc_doc.get("value", 0))

    return render_template(
        "cart.html",
        is_logged_in=is_logged_in,
        platform_fee=platform_fee,
        free_delivery_threshold=FREE_DELIVERY_THRESHOLD,
        delivery_fee=DELIVERY_FEE,
        handling_fee=handling_fee, # Passed to HTML
        surge_charge=surge_charge,  # Passed to HTML
        shop=shop,
        is_shop_closed=is_shop_closed
    )
    
@app.route("/address", methods=["GET"])
def address_page():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    # Check store online status
    shop_id = session.get("current_shop_id")
    if shop_id:
        try:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
            if shop and shop.get("is_online") is False:
                flash(f"{shop.get('shop_name', 'Store')} is currently closed and not taking orders.", "warning")
                return redirect(url_for("cart_page"))
        except Exception:
            pass

    user_doc = users.find_one({"_id": ObjectId(user_id), "role": "customer"})
    saved_addresses = user_doc.get("addresses", []) if user_doc else []

    if user_doc and not session.get("user_phone"):
        session["user_phone"] = user_doc.get("phone", "")

    society = None
    current_society_id = session.get("current_society_id")
    if current_society_id:
        try:
            society = societies.find_one({"_id": ObjectId(current_society_id)})
        except Exception:
            pass
    if not society:
        society = societies.find_one()

    return render_template("address.html", saved_addresses=saved_addresses, society=society, user=user_doc)


@app.route("/my-orders")
def my_orders():
    # Agar user login nahi hai to login par bhejo
    user_id = session.get("user_id")
    user_phone = session.get("user_phone")

    if not user_id and not user_phone:
        return redirect(url_for("login"))

    query = {}
    # Pehle user_id se match karo (zyada reliable)
    if user_id:
        query["user_id"] = user_id
    # Safety ke liye phone bhi include kar sakte ho (same number se purane orders)
    if user_phone:
        query["user_phone"] = user_phone

    orders_cursor = orders.find(query).sort("created_at", -1)
    orders_list = list(orders_cursor)

    return render_template("my_orders.html", orders=orders_list)

@app.route("/order/<order_id>")
def order_detail(order_id):
    user_id = session.get("user_id")
    user_phone = session.get("user_phone")

    if not user_id and not user_phone:
        return redirect(url_for("login"))

    order = orders.find_one({"order_id": order_id})
    if not order:
        return redirect(url_for("my_orders"))

    # Status logic: only 2 statuses (RECEIVED and DELIVERED, plus CANCELLED)
    raw = (order.get("status") or "pending").lower()
    if raw in ["delivered"]:
        status = "DELIVERED"
        is_delivered = True
        is_cancelled = False
        step_index = 1
    elif raw in ["cancelled", "canceled"]:
        status = "CANCELLED"
        is_delivered = False
        is_cancelled = True
        step_index = 1
    else:
        status = "RECEIVED"
        is_delivered = False
        is_cancelled = False
        step_index = 0

    return render_template(
        "order_detail.html",
        order=order,
        status=status,
        is_cancelled=is_cancelled,
        is_delivered=is_delivered,
        step_index=step_index,
    )


@app.route("/api/order-poll/<order_id>")
def customer_api_order_poll(order_id):
    order = orders.find_one({"order_id": order_id})
    if not order:
        return jsonify({"success": False, "message": "Order not found"}), 404

    subtotal = float(order.get("subtotal") or 0.0)
    total = float(order.get("total") or order.get("total_amount") or 0.0)
    raw_status = (order.get("status") or "pending").lower()

    return jsonify({
        "success": True,
        "order_id": order_id,
        "status": raw_status,
        "subtotal": round(subtotal, 2),
        "total": round(total, 2),
        "notes_available": order.get("notes_available"),
        "notes_price": round(float(order.get("notes_price") or 0.0), 2),
        "notes_items": [
            it for it in order.get("notes_items", [])
            if not _is_noise_note_text(it.get("name"))
        ],
        "notes_finalized": bool(order.get("notes_finalized")),
        "items": [
            {
                "name": it.get("name"),
                "qty": it.get("qty", 1),
                "price": float(it.get("price", 0)),
                "unit": it.get("unit", ""),
                "is_note_item": bool(it.get("is_note_item")),
                "image": it.get("image", "")
            }
            for it in order.get("items", [])
            if not _is_noise_note_text(it.get("name"))
        ]
    })


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("user_phone", None)
    session.pop("user_name", None)
    return redirect(url_for("index"))

@app.route("/order-confirmation")
def order_confirmation():
    order_id = request.args.get("order_id")
    if not order_id:
        return redirect(url_for("index"))

    order = orders.find_one({"order_id": order_id})
    if not order:
        return redirect(url_for("index"))

    return render_template("order_confirmation.html", order=order)

@app.route("/admin/settings/fees", methods=["GET", "POST"])
def admin_fees_settings():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    # Settings fetch karein
    pf = settings.find_one({"key": "platform_fee"}) or {"value": 0}
    thr = settings.find_one({"key": "free_delivery_threshold"}) or {"value": 49}
    df = settings.find_one({"key": "delivery_fee"}) or {"value": 10}
    hf = settings.find_one({"key": "handling_fee"}) or {"value": 0} # NEW
    sc = settings.find_one({"key": "surge_charge"}) or {"value": 0} # NEW

    if request.method == "POST":
        raw_pf = request.form.get("platform_fee", "0").strip()
        raw_thr = request.form.get("free_delivery_threshold", "49").strip()
        raw_df = request.form.get("delivery_fee", "10").strip()
        raw_hf = request.form.get("handling_fee", "0").strip() # NEW
        raw_sc = request.form.get("surge_charge", "0").strip() # NEW
        
        try:
            pf_val = int(raw_pf)
            thr_val = int(raw_thr)
            df_val = int(raw_df)
            hf_val = int(raw_hf) # NEW
            sc_val = int(raw_sc) # NEW
            
            if any(v < 0 for v in [pf_val, thr_val, df_val, hf_val, sc_val]):
                raise ValueError
        except ValueError:
            return render_template(
                "admin_fees_settings.html",
                error="Please enter valid non-negative amounts.",
                current_platform_fee=pf.get("value", 0),
                current_threshold=thr.get("value", 49),
                current_delivery_fee=df.get("value", 10),
                current_handling_fee=hf.get("value", 0),
                current_surge_charge=sc.get("value", 0)
            )

        # Update Database
        settings.update_one({"key": "platform_fee"}, {"$set": {"value": pf_val}}, upsert=True)
        settings.update_one({"key": "free_delivery_threshold"}, {"$set": {"value": thr_val}}, upsert=True)
        settings.update_one({"key": "delivery_fee"}, {"$set": {"value": df_val}}, upsert=True)
        settings.update_one({"key": "handling_fee"}, {"$set": {"value": hf_val}}, upsert=True) # NEW
        settings.update_one({"key": "surge_charge"}, {"$set": {"value": sc_val}}, upsert=True) # NEW

        return render_template(
            "admin_fees_settings.html",
            success="All financial rules updated successfully.",
            current_platform_fee=pf_val,
            current_threshold=thr_val,
            current_delivery_fee=df_val,
            current_handling_fee=hf_val,
            current_surge_charge=sc_val,
        )

    return render_template(
        "admin_fees_settings.html",
        current_platform_fee=pf.get("value", 0),
        current_threshold=thr.get("value", 49),
        current_delivery_fee=df.get("value", 10),
        current_handling_fee=hf.get("value", 0),
        current_surge_charge=sc.get("value", 0)
    )
    
@app.route("/portal/refresh-captcha", methods=["POST"])
def portal_refresh_captcha():
    # 4-digit random code
    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return jsonify({"captcha_code": code})

@app.route("/admin/orders")
def admin_orders():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    all_orders = list(orders.find().sort("created_at", -1))
    all_vendors = list(users.find({"role": "vendor"}, {"_id": 1, "name": 1, "shop_name": 1, "phone": 1}))
    all_delivery = list(users.find({"role": "delivery"}, {"_id": 1, "name": 1, "phone": 1}))

    # Calculate Overview Metrics
    total_orders = len(all_orders)
    pending_orders = sum(1 for o in all_orders if (o.get("status") or "pending").lower() not in ["delivered", "cancelled", "canceled"])
    delivered_orders = sum(1 for o in all_orders if (o.get("status") or "").lower() == "delivered")
    cancelled_orders = sum(1 for o in all_orders if (o.get("status") or "").lower() in ["cancelled", "canceled"])
    
    total_revenue = sum(float(o.get("total", 0) or 0) for o in all_orders if (o.get("status") or "").lower() not in ["cancelled", "canceled"])

    # Calculate Today's figures in IST
    today_date = datetime.datetime.now(IST).date()
    today_orders_count = 0
    today_revenue = 0.0
    for o in all_orders:
        cat = o.get("created_at")
        if cat and isinstance(cat, (datetime.datetime, datetime.date)):
            if getattr(cat, "tzinfo", None) is None:
                cat = pytz.utc.localize(cat)
            if cat.astimezone(IST).date() == today_date:
                today_orders_count += 1
                if (o.get("status") or "").lower() not in ["cancelled", "canceled"]:
                    today_revenue += float(o.get("total", 0) or 0)

    overview = {
        "total": total_orders,
        "pending": pending_orders,
        "delivered": delivered_orders,
        "cancelled": cancelled_orders,
        "total_revenue": round(total_revenue, 2),
        "today_orders": today_orders_count,
        "today_revenue": round(today_revenue, 2)
    }

    return render_template(
        "admin_orders.html",
        orders=all_orders,
        vendors=all_vendors,
        delivery_boys=all_delivery,
        overview=overview
    )

@app.route("/admin/api/orders-feed")
def admin_api_orders_feed():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    all_orders = list(orders.find().sort("created_at", -1))
    total_orders = len(all_orders)
    pending_orders = sum(1 for o in all_orders if (o.get("status") or "pending").lower() not in ["delivered", "cancelled", "canceled"])
    delivered_orders = sum(1 for o in all_orders if (o.get("status") or "").lower() == "delivered")
    cancelled_orders = sum(1 for o in all_orders if (o.get("status") or "").lower() in ["cancelled", "canceled"])
    total_revenue = sum(float(o.get("total", 0) or 0) for o in all_orders if (o.get("status") or "").lower() not in ["cancelled", "canceled"])

    today_date = datetime.datetime.now(IST).date()
    today_orders_count = 0
    today_revenue = 0.0
    for o in all_orders:
        cat = o.get("created_at")
        if cat and isinstance(cat, (datetime.datetime, datetime.date)):
            if getattr(cat, "tzinfo", None) is None:
                cat = pytz.utc.localize(cat)
            if cat.astimezone(IST).date() == today_date:
                today_orders_count += 1
                if (o.get("status") or "").lower() not in ["cancelled", "canceled"]:
                    today_revenue += float(o.get("total", 0) or 0)

    order_ids = [str(o.get("order_id")) for o in all_orders]

    return jsonify({
        "status": "success",
        "total": total_orders,
        "pending": pending_orders,
        "delivered": delivered_orders,
        "cancelled": cancelled_orders,
        "total_revenue": round(total_revenue, 2),
        "today_orders": today_orders_count,
        "today_revenue": round(today_revenue, 2),
        "order_ids": order_ids
    })

@app.route("/admin/api/register-push-token", methods=["POST"])
def admin_register_push_token():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.get_json() or {}
    token = data.get("token")
    subscription = data.get("subscription")
    token_type = data.get("type", "fcm" if token else "webpush")
    user_agent = request.headers.get("User-Agent", "")

    if not token and not subscription:
        return jsonify({"success": False, "message": "Token or subscription required"}), 400

    endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
    filter_query = {"token": token} if token else {"subscription.endpoint": endpoint}

    admin_push_subscriptions.update_one(
        filter_query,
        {
            "$set": {
                "token": token,
                "subscription": subscription,
                "type": token_type,
                "user_agent": user_agent,
                "active": True,
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }
        },
        upsert=True
    )
    return jsonify({"success": True, "message": "Admin device token registered for closed-browser alerts"})

@app.route("/admin/api/test-push-notification", methods=["POST"])
def admin_test_push_notification():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    sample_order = {
        "order_id": f"TEST-{str(uuid.uuid4())[:8]}",
        "user_name": "Test Resident (Flat A-402)",
        "total": 499,
        "items": [
            {"name": "Amul Taaza Milk 500ml", "qty": 2, "price": 27},
            {"name": "Aashirvaad Shudh Chakki Atta 5kg", "qty": 1, "price": 245}
        ],
        "status": "pending"
    }
    send_admin_push_notifications_async(sample_order)
    return jsonify({"success": True, "message": "Test cloud push alert dispatched to all registered admin devices!"})

@app.route("/admin/order/<order_id>")
def admin_order_detail(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    # Support string, integer, or ObjectId order_id lookup
    order = orders.find_one({"order_id": order_id})
    if not order and str(order_id).isdigit():
        order = orders.find_one({"order_id": int(order_id)})
    if not order:
        try:
            order = orders.find_one({"_id": ObjectId(order_id)})
        except Exception:
            pass

    if not order:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "message": "Order not found"}), 404
        flash("Order not found", "error")
        return redirect(url_for("admin_orders"))

    all_vendors = list(users.find({"role": "vendor"}, {"_id": 1, "name": 1, "shop_name": 1, "phone": 1}))
    all_delivery = list(users.find({"role": "delivery"}, {"_id": 1, "name": 1, "phone": 1}))

    # If requested via AJAX from No-Reload SPA, return the clean snippet template
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.args.get("partial") == "1":
        return render_template(
            "admin_order_detail_snippet.html",
            order=order,
            o=order,
            vendors=all_vendors,
            delivery_boys=all_delivery
        )

    # If direct browser load or tab reload on /admin/order/<order_id>, redirect to seamless SPA orders view with order open
    return redirect(url_for("admin_orders", order=order_id))

@app.route("/admin/order/<order_id>/assign-vendor", methods=["POST"])
def admin_assign_vendor(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    vendor_id = request.form.get("vendor_id") or None
    vendor_user = None
    if vendor_id:
        try:
            vendor_user = users.find_one({"_id": ObjectId(vendor_id), "role": "vendor"})
        except Exception:
            pass

    update = {"$set": {
        "vendor_id": ObjectId(vendor_id) if (vendor_id and vendor_user) else None,
        "vendor_status": "assigned" if (vendor_id and vendor_user) else "pending"
    }}
    orders.update_one({"order_id": order_id}, update)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
        return jsonify({
            "success": True,
            "order_id": order_id,
            "vendor_id": str(vendor_id) if vendor_id else None,
            "vendor_name": vendor_user.get("shop_name") or vendor_user.get("name") if vendor_user else None,
            "message": "Vendor updated successfully"
        })
    return redirect(url_for("admin_orders"))

@app.route("/admin/order/<order_id>/assign-delivery", methods=["POST"])
def admin_assign_delivery(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    delivery_id = request.form.get("delivery_id") or None
    delivery_user = None
    if delivery_id:
        try:
            delivery_user = users.find_one({"_id": ObjectId(delivery_id), "role": "delivery"})
        except Exception:
            pass

    update = {"$set": {
        "delivery_id": ObjectId(delivery_id) if (delivery_id and delivery_user) else None,
        "delivery_status": "assigned" if (delivery_id and delivery_user) else None
    }}
    orders.update_one({"order_id": order_id}, update)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
        return jsonify({
            "success": True,
            "order_id": order_id,
            "delivery_id": str(delivery_id) if delivery_id else None,
            "delivery_name": delivery_user.get("name") if delivery_user else None,
            "delivery_phone": delivery_user.get("phone") if delivery_user else None,
            "message": f"Assigned to {delivery_user.get('name')}" if delivery_user else "Rider unassigned"
        })
    return redirect(url_for("admin_orders"))

@app.route("/admin/order/<order_id>/status", methods=["POST"])
def admin_update_status(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    allowed_statuses = {
        "RECEIVED",
        "PACKED",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
        "CANCELLED",
    }

    new_status = (request.form.get("status", "") or "").strip().upper()

    if new_status in allowed_statuses:
        update = {"status": new_status}
        if new_status == "DELIVERED":
            update["delivered_at"] = datetime.datetime.now(datetime.timezone.utc)
            update["delivery_status"] = "delivered"
            try:
                curr_order = orders.find_one({"order_id": order_id})
                if curr_order and not curr_order.get("delivery_whatsapp_sent"):
                    update["delivery_whatsapp_sent"] = True
                    order_delivered_data = dict(curr_order)
                    order_delivered_data.update(update)
                    send_order_delivery_whatsapp_async(order_delivered_data)
            except Exception as _wh_err:
                print("[Admin Delivery WhatsApp Dispatch Error]:", _wh_err)
        elif new_status == "OUT_FOR_DELIVERY":
            update["delivery_status"] = "out_for_delivery"
        elif new_status == "CANCELLED":
            cancel_reason = request.form.get("cancel_reason", "Cancelled by Admin").strip()
            update["cancel_reason"] = cancel_reason
            update["delivery_status"] = "cancelled"
            try:
                curr_order = orders.find_one({"order_id": order_id})
                if curr_order:
                    reverse_order_store_credit(db, curr_order, reason=f"Admin Cancelled ({cancel_reason})")
            except Exception as _rev_err:
                print("[Khata Admin Reversal Error]:", _rev_err)

        orders.update_one(
            {"order_id": order_id},
            {"$set": update},
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({
                "success": True,
                "order_id": order_id,
                "new_status": new_status,
                "message": f"Order #{order_id[-8:]} status updated to {new_status}"
            })

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
        return jsonify({"success": False, "message": "Invalid status parameter"}), 400

    return redirect(url_for("admin_orders"))

@app.route("/admin/order/<order_id>/delete", methods=["POST"])
def admin_delete_order(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    try:
        del_order = orders.find_one({"order_id": order_id})
        if del_order:
            reverse_order_store_credit(db, del_order, reason="Admin Order Deleted")
    except Exception as _del_err:
        print("[Khata Delete Reversal Error]:", _del_err)

    orders.delete_one({"order_id": order_id})

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
        return jsonify({
            "success": True,
            "order_id": order_id,
            "message": f"Order #{order_id[-8:]} removed permanently"
        })

    return redirect(url_for("admin_orders"))

from bson import ObjectId

@app.route("/admin/order/<order_id>/assign-vendor-items", methods=["POST"])
def admin_assign_vendor_items(order_id):
    if not session.get("admin_logged_in"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    # Order fetch karo
    order = orders.find_one({"order_id": order_id})
    if not order:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Order not found"}), 404
        return redirect(url_for("admin_orders"))

    items = order.get("items", [])
    if not isinstance(items, list):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
            return jsonify({"success": False, "message": "Invalid order items"}), 400
        return redirect(url_for("admin_orders"))

    # Har item ke liye form se vendor lo
    updated_items = []
    for idx, item in enumerate(items):
        field_name = f"item_vendor_{idx}"
        raw_vid = request.form.get(field_name, "").strip()

        if raw_vid:
            try:
                item["vendor_id"] = ObjectId(raw_vid)
            except Exception:
                item.pop("vendor_id", None)
        else:
            item.pop("vendor_id", None)

        updated_items.append(item)

    # Order ka items array update karo
    orders.update_one(
        {"order_id": order_id},
        {"$set": {"items": updated_items}}
    )

    has_any_vendor = any(it.get("vendor_id") for it in updated_items)
    all_assigned = all(it.get("vendor_id") for it in updated_items) if updated_items else False
    vendor_status = "assigned" if all_assigned else ("partial" if has_any_vendor else "pending")

    orders.update_one(
        {"order_id": order_id},
        {"$set": {"vendor_status": vendor_status}}
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1":
        return jsonify({
            "success": True,
            "order_id": order_id,
            "vendor_status": vendor_status,
            "message": "Store stock distribution matrix saved successfully."
        })

    return redirect(url_for("admin_orders"))

@app.route("/portal/order/<order_id>/vendor-status", methods=["POST"])
def portal_update_vendor_status(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")

    # sirf vendor ko allow
    if role != "vendor" or not portal_user_id:
        return redirect(url_for("vendor_login"))

    new_status = request.form.get("vendor_status", "").strip()
    allowed = {"received", "processing", "packed", "ready", "out_for_delivery", "delivered"}

    if new_status in allowed:
        update_fields = {"vendor_status": new_status, "status": new_status}
        if new_status == "delivered":
            update_fields["delivered_at"] = datetime.datetime.now(datetime.timezone.utc)
            try:
                curr_order = orders.find_one({"order_id": order_id})
                if curr_order and not curr_order.get("delivery_whatsapp_sent"):
                    update_fields["delivery_whatsapp_sent"] = True
                    order_delivered_data = dict(curr_order)
                    order_delivered_data.update(update_fields)
                    send_order_delivery_whatsapp_async(order_delivered_data)
            except Exception as _wh_err:
                print("[Vendor Delivery WhatsApp Dispatch Error]:", _wh_err)
            
        orders.update_one(
            {"order_id": order_id},
            {"$set": update_fields}
        )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"success": True, "order_id": order_id, "vendor_status": new_status})

    return redirect(url_for("vendor_dashboard"))

@app.route("/portal/order/<order_id>/delivery-status", methods=["POST"])
def portal_update_delivery_status(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")

    # sirf delivery role allow
    if role != "delivery" or not portal_user_id:
        return redirect(url_for("logistics_login"))

    new_status = request.form.get("delivery_status", "").strip()
    allowed = {"accepted", "picked", "out_for_delivery", "delivered"}

    if new_status in allowed:
        update_doc = {"delivery_status": new_status, "status": new_status}
        if new_status == "delivered":
            update_doc["delivered_at"] = datetime.datetime.now(datetime.timezone.utc)
            try:
                curr_order = orders.find_one({"order_id": order_id})
                if curr_order and not curr_order.get("delivery_whatsapp_sent"):
                    update_doc["delivery_whatsapp_sent"] = True
                    order_delivered_data = dict(curr_order)
                    order_delivered_data.update(update_doc)
                    send_order_delivery_whatsapp_async(order_delivered_data)
            except Exception as _wh_err:
                print("[Logistics Delivery WhatsApp Dispatch Error]:", _wh_err)
        orders.update_one(
            {
                "order_id": order_id,
                "delivery_id": ObjectId(portal_user_id),
            },
            {"$set": update_doc},
        )

    return redirect(url_for("logistics_dashboard"))

@app.route("/admin/command-center/partial")
def admin_command_center_partial():
    if not session.get("admin_logged_in"):
        return "", 401
    orders_list = list(orders.find().sort("created_at", -1))
    vendors = list(users.find({"role": "vendor"}))
    delivery_boys = list(users.find({"role": "delivery"}))
    return render_template(
        "admin_command_center_partial.html",  # sirf {% for o in orders %} ... {% endfor %}
        orders=orders_list,
        vendors=vendors,
        delivery_boys=delivery_boys,
    )

@app.route("/portal/vendor-history")
def portal_vendor_history():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return redirect(url_for("vendor_login"))

    vendoroid = ObjectId(portal_user_id)

    all_vendor_orders = list(
        orders.find({"items.vendor_id": vendoroid}).sort("created_at", -1)
    )

    # Per-day income aggregate
    from collections import defaultdict
    daily = defaultdict(lambda: {"total": 0.0, "orders": 0})

    for o in all_vendor_orders:
        created = o.get("created_at")
        if not created:
            continue
        day = created.date()
        amt = 0.0
        for it in o.get("items", []):
            if it.get("vendor_id") == vendoroid:
                amt += float(it.get("price", 0)) * int(it.get("qty", 1))
        if amt > 0:
            daily[day]["total"] += amt
            daily[day]["orders"] += 1

    # sort by date desc
    daily_list = sorted(
        [{"date": d, "total": v["total"], "orders": v["orders"]} for d, v in daily.items()],
        key=lambda x: x["date"],
        reverse=True,
    )

    return render_template(
        "vendor_history.html",
        daily_list=daily_list,
    )

# ================================
# ADMIN: INVENTORY STATUS (OUT OF STOCK)
# ================================

@app.route("/admin/inventory")
def admin_inventory():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    # saare products load karo, naam ke hisaab se sort
    items = list(products.find().sort("name", 1))
    try:
        categories_list = get_cached_categories()
    except Exception:
        categories_list = []
    return render_template("portal_inventory.html", products=items, categories=categories_list)

@app.route("/admin/inventory/toggle/<product_id>", methods=["POST"])
def portal_inventory_toggle(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.form.get("is_ajax") == "1"

    if not session.get("admin_logged_in"):
        if is_ajax:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        return redirect(url_for("admin_login"))

    next_status = request.form.get("next_status")
    if next_status not in ("oos", "instock"):
        if is_ajax:
            return jsonify({"success": False, "message": "Invalid stock parameter"}), 400
        return redirect(url_for("admin_inventory"))

    out_of_stock = (next_status == "oos")
    try:
        products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {"out_of_stock": out_of_stock}}
        )
        if is_ajax:
            return jsonify({
                "success": True,
                "product_id": str(product_id),
                "out_of_stock": out_of_stock,
                "status": "oos" if out_of_stock else "instock",
                "message": "Stock fulfillment status updated."
            })
    except Exception as e:
        if is_ajax:
            return jsonify({"success": False, "message": str(e)}), 500

    return redirect(url_for("admin_inventory"))

@app.route("/admin/api/product/<product_id>", methods=["GET"])
def api_get_product_detail(product_id):
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Authentication required"}), 401
    try:
        p = products.find_one({"_id": ObjectId(product_id)})
        if not p:
            return jsonify({"success": False, "message": "Product not found"}), 404
        return jsonify({
            "success": True,
            "product": {
                "_id": str(p["_id"]),
                "name": p.get("name", ""),
                "price": float(p.get("price", 0)),
                "discount_price": float(p.get("discount_price")) if p.get("discount_price") else None,
                "unit": p.get("unit", ""),
                "category": p.get("category", ""),
                "brand": p.get("brand", ""),
                "description": p.get("description", ""),
                "out_of_stock": bool(p.get("out_of_stock", False)),
                "image": p.get("image", "")
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/admin/service-toggle", methods=["POST"])
def admin_service_toggle():
    # session key: "admin_logged_in"
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    
    mode = request.form.get("mode")  # "on" / "off"
    val = True if mode == "on" else False

    settings.update_one(
        {"key": "maintenance_mode"},
        {"$set": {"value": val}},
        upsert=True,
    )
    # route function name: admin_dashboard
    return redirect(url_for("admin_dashboard"))

@app.before_request
def check_maintenance_mode():
    path = request.path or ""

    # admin aur static ko always allow
    if path.startswith("/admin") or path.startswith("/static"):
        return

    if is_maintenance_mode():
        # checkout / APIs ke liye JSON error
        if path.startswith("/checkout"):
            return jsonify({"error": "Service temporarily unavailable"}), 503
        # baaki pages ke liye HTML page
        return render_template("maintenance.html"), 503

@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    maintenance_on = is_maintenance_mode()
    return render_template("admin_dashboard.html", maintenance_on=maintenance_on)

@app.route("/twilio/connect-customer/<order_id>", methods=["POST"])
def twiml_connect_customer(order_id):
    """
    Twilio yahan POST karega jab delivery boy call uthayega.
    Yahan se customer ko dial karke bridge kar denge.
    """
    order = orders.find_one({"order_id": order_id})
    if not order:
        resp = VoiceResponse()
        resp.say("Sorry, this order could not be found.")
        return str(resp), 200

    customer_phone = order.get("user_phone")
    if not customer_phone:
        resp = VoiceResponse()
        resp.say("Customer phone is missing for this order.")
        return str(resp), 200

    resp = VoiceResponse()
    # Proxy number se customer ko call, callerId hamesha proxy hi rahega
    with resp.dial(callerId=TW_PROXY_NUM) as dial:
        dial.number(customer_phone)

    return str(resp), 200

BASE_URL = os.getenv("BASE_URL", "")  # e.g. https://abcd1234.ngrok.io ya https://yourdomain.com

@app.route("/portal/delivery/call-customer", methods=["POST"])
def delivery_call_customer():
    """
    Delivery boy portal se AJAX ke through call hoga.
    Ye Twilio ko instruct karega ki:
      1) Proxy number se delivery boy ko call karo
      2) Uthate hi /twilio/connect-customer/<order_id> se customer ko bridge karo
    """
    role = session.get("user_role")
    delivery_user_id = session.get("portal_user_id")

    if role != "delivery" or not delivery_user_id:
        return jsonify({"error": "Unauthorized"}), 403

    if not TW_PROXY_NUM:
        return jsonify({"error": "Proxy number not configured"}), 500

    data = request.get_json() or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    order = orders.find_one({"order_id": order_id})
    if not order:
        return jsonify({"error": "Order not found"}), 404

    customer_phone = order.get("user_phone")
    if not customer_phone:
        return jsonify({"error": "Customer phone missing"}), 400

    # delivery boy ka phone users collection se
    try:
        delivery_doc = users.find_one(
            {"_id": ObjectId(delivery_user_id), "role": "delivery"}
        )
    except Exception:
        delivery_doc = None

    if not delivery_doc or not delivery_doc.get("phone"):
        return jsonify({"error": "Delivery phone not configured"}), 400

    delivery_phone = delivery_doc["phone"]

    # PUBLIC callback URL: BASE_URL + route path
    if not BASE_URL:
        return jsonify({"error": "BASE_URL not configured"}), 500

    callback_url = f"{BASE_URL}{url_for('twiml_connect_customer', order_id=order_id)}"

    try:
        call = twilio_client.calls.create(
            from_=TW_PROXY_NUM,
            to=delivery_phone,  # pehle delivery boy ko call
            url=callback_url,
        )
        return jsonify({"message": "Call initiated", "sid": call.sid})
    except Exception:
        return jsonify({"error": "Could not initiate call"}), 500

@app.route("/my-profile")
def my_profile():
    user_id = session.get("user_id")
    user_phone = session.get("user_phone")

    if not user_id and not user_phone:
        return redirect(url_for("login"))

    # primary lookup by _id + role=customer
    user_doc = None
    if user_id:
        try:
            user_doc = users.find_one({"_id": ObjectId(user_id), "role": "customer"})
        except Exception:
            user_doc = None

    # fallback by phone (agar purane accounts me id na ho)
    if not user_doc and user_phone:
        user_doc = users.find_one({"phone": user_phone, "role": "customer"})

    if not user_doc:
        return redirect(url_for("index"))

    saved_addresses = user_doc.get("addresses", []) or []

    # total orders count
    orders_count = orders.count_documents({
        "$or": [
            {"user_id": user_id} if user_id else {},
            {"user_phone": user_phone} if user_phone else {},
        ]
    })

    # fetch current active shop so header remains consistent
    shop = None
    shop_id = session.get("current_shop_id")
    if shop_id:
        try:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            shop = None

    return render_template(
        "my_profile.html",
        user=user_doc,
        saved_addresses=saved_addresses,
        orders_count=orders_count,
        shop=shop,
    )

@app.route("/portal/delivery/<order_id>/qr")
def delivery_qr(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "delivery" or not portal_user_id:
        return redirect(url_for("logistics_login"))

    order = orders.find_one({"order_id": order_id})
    if not order:
        return redirect(url_for("logistics_dashboard"))

    amount = float(order.get("total", 0) or 0.0)

    # FIXED UPI ID + NAME
    UPI_VPA = "8545816135@slc"      # yahan apna real VPA
    UPI_NAME = "Ankush Sachan"         # display name

    qr_src, upi_url = build_upi_qr(
        vpa=UPI_VPA,
        name=UPI_NAME,
        amount=amount,
        note=f"Order {order_id}",
    )

    return render_template(
        "delivery_qr.html",
        order=order,
        qr_src=qr_src,
        upi_url=upi_url,
        amount=amount,
    )

@app.route("/support", methods=["GET", "POST"])
def support():
    user_id = session.get("user_id")
    user_phone = session.get("user_phone")
    user_name = session.get("user_name")

    if request.method == "POST":
        msg = request.get_json() or {}
        text = (msg.get("text") or "").strip().lower()

        def has(*keys):
            return any(k in text for k in keys)

        # intent mapping
        if has("refund", "money back", "payment reverse"):
            reply = (
                "Refunds are processed within 24–48 hours after approval. "
                "If you paid via UPI/card, amount goes back to the same source."
            )
        elif has("late", "delay", "where is my order", "kab aayega"):
            reply = (
                "Delivery can sometimes get delayed due to traffic or weather. "
                "If it has been more than 45 minutes, reply with your order ID and we’ll check."
            )
        elif has("cancel", "order cancel", "cancelled"):
            reply = (
                "Orders can be cancelled until they are marked as 'Out for Delivery'. "
                "Share your order ID here; if it’s still early, we’ll try to cancel."
            )
        elif has("wrong item", "missing item", "item missing", "galat saman"):
            reply = (
                "Sorry about that. Please type your order ID and mention which item is wrong/missing. "
                "Our team will review and arrange a replacement or refund."
            )
        elif has("payment", "upi", "failed", "double charged", "duplicate"):
            reply = (
                "If payment failed but money is deducted, it usually auto‑reverses within 24–72 hours. "
                "If not, share transaction reference and order ID."
            )
        elif has("address", "change address", "wrong address"):
            reply = (
                "Address can be updated until the order is packed. "
                "Send your order ID and new address in one message, we’ll check feasibility."
            )
        else:
            reply = (
                "Thanks for your message. A support executive will review this soon. "
                "For urgent issues, you can tap 'Chat on WhatsApp' below for faster help."
            )

        # optional: log conversation for future training / analysis
        try:
            db.support_messages.insert_one(
                {
                    "user_id": user_id,
                    "user_phone": user_phone,
                    "user_name": user_name,
                    "text": text,
                    "reply": reply,
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                }
            )
        except Exception:
            pass

        return jsonify({"reply": reply})

    return render_template("support.html", user_name=user_name, user_phone=user_phone)

@app.route("/admin/service-area", methods=["GET", "POST"])
def admin_service_area():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    doc = settings.find_one({"key": "service_area"}) or {}
    center = doc.get("center") or {}
    current_lat = center.get("lat")
    current_lng = center.get("lng")
    current_radius = doc.get("radius_km", 0)

    if request.method == "POST":
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()
        raw_radius = request.form.get("radius_km", "").strip()

        error = None
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
            radius_km = float(raw_radius)
            if radius_km <= 0:
                error = "Radius must be greater than 0."
        except ValueError:
            error = "Please enter valid numeric latitude, longitude and radius."

        if error:
            return render_template(
                "admin_service_area.html",
                error=error,
                lat=raw_lat,
                lng=raw_lng,
                radius_km=raw_radius or "",
            )

        settings.update_one(
            {"key": "service_area"},
            {
                "$set": {
                    "center": {"lat": lat, "lng": lng},
                    "radius_km": radius_km,
                }
            },
            upsert=True,
        )

        return redirect(url_for("admin_service_area"))

    return render_template(
        "admin_service_area.html",
        lat=current_lat or "",
        lng=current_lng or "",
        radius_km=current_radius or "",
    )

@app.route('/api/service-config')
def service_config():
    """API endpoint for frontend service area check"""
    doc = settings.find_one({"key": "service_area"}) or {}
    center = doc.get("center") or {}
    
    return jsonify({
        'lat': center.get('lat'),
        'lng': center.get('lng'),
        'radius_km': doc.get('radius_km', 0)
    })

@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_json():
    return jsonify({}), 200

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

@app.errorhandler(Exception) # Yeh line har tarah ke error ko catch karegi
def handle_exception(e):
    from werkzeug.exceptions import HTTPException

    # 1. Check HTTP status code
    if isinstance(e, HTTPException):
        code = e.code
    elif hasattr(e, 'code'):
        code = e.code
    else:
        # 2. Agar koi python code crash hua hai (jaise Database failure ya Bug)
        code = 500

    # Only log traceback and write to crash.log for actual server errors (500+)
    if code >= 500:
        import traceback
        traceback.print_exc()
        try:
            with open("crash.log", "w") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
    
    # Custom Messages mapping
    messages = {
        404: {"title": "Page Not Found", "desc": "The link might be broken or moved."},
        403: {"title": "Access Denied", "desc": "You don't have permission to see this."},
        401: {"title": "Unauthorized", "desc": "Please login to access this page."},
        500: {"title": "System Glitch", "desc": "Something went wrong on our end. We're fixing it!"},
        503: {"title": "Service Busy", "desc": "Server is overloaded or under maintenance."},
    }

    err_info = messages.get(code, {"title": "Unexpected Error", "desc": "An unknown error occurred."})

    # API/JSON Requests ke liye JSON return karo
    if request.path.startswith('/api/') or request.headers.get('Content-Type') == 'application/json':
        return jsonify({"error": err_info['title'], "status": code}), code

    # Baaki sab ke liye error.html dikhao
    return render_template(
        "error.html", 
        code=code, 
        title=err_info['title'], 
        desc=err_info['desc']
    ), code
 
@app.route("/privacy") 
def privacy(): 
    return render_template("privacy.html")   


   
from flask import render_template, abort
# Baki imports ke saath ise bhi rakhein

@app.route('/download-invoice/<order_id>')
def download_invoice(order_id):
    """📄 Jippy Store Premium Invoice Route"""
    try:
        # 1. Database se order fetch karein
        # Agar aap MongoDB use kar rahe hain toh:
        order = orders.find_one({"order_id": order_id})
        
        if not order:
            return "Order not found", 404
            
        # 2. Check karein ki order DELIVERED hai ya nahi (Safety Check)
        raw_status = (order.get('status') or 'pending').lower()
        if raw_status != 'delivered':
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.args.get("json") == "1":
                return jsonify({
                    "success": False,
                    "error": "invoice_not_ready",
                    "status": raw_status,
                    "message": f"Invoice is only available for DELIVERED orders. Current status is {raw_status.upper()}."
                }), 400

            return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tax Invoice Not Available - Jippy Store</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap" rel="stylesheet">
</head>
<body class="bg-[#f1f3f6] font-['Roboto',sans-serif] min-h-screen flex items-center justify-center p-4">
  <div class="max-w-md w-full bg-white rounded-sm shadow-md border border-[#e0e0e0] p-6 text-center space-y-4">
    <div class="w-14 h-14 mx-auto rounded-full bg-amber-50 text-amber-500 border border-amber-200 flex items-center justify-center text-2xl">
      <i class="fas fa-file-invoice"></i>
    </div>
    <div>
      <h2 class="text-lg font-bold text-[#212121]">Tax Invoice Not Generated Yet</h2>
      <p class="text-xs text-[#878787] mt-1">
        Order <strong>#{order_id[-12:]}</strong> is currently: <span class="uppercase font-bold text-amber-600">{raw_status.replace('_', ' ')}</span>.
      </p>
    </div>
    <div class="p-3 bg-amber-50/70 border border-amber-200/80 rounded text-left text-xs text-amber-900 space-y-1">
      <div class="font-bold flex items-center gap-1.5 text-amber-800">
        <i class="fas fa-circle-exclamation text-xs"></i>
        <span>Tax Invoice Policy:</span>
      </div>
      <p class="text-[11px] text-amber-800/90 leading-relaxed">
        Official tax invoices are finalized and generated only after the order is delivered to the customer.
      </p>
    </div>
    <div class="pt-2">
      <button onclick="window.close(); if(history.length > 1) history.back();" class="px-5 py-2 bg-[#2874f0] hover:bg-blue-600 text-white text-xs font-bold rounded-sm transition-all shadow-xs cursor-pointer">
        ← Close Window
      </button>
    </div>
  </div>
</body>
</html>""", 403

        # 3. Invoice template render karein
        order_items = order.get('items', []) if isinstance(order, dict) else getattr(order, 'items', [])
        return render_template("invoice.html", order=order, items=order_items)

    except Exception as e:
        print(f"❌ Invoice Error: {e}")
        return "Internal Server Error", 500

# ─── INVOICE PDF DOWNLOAD BRIDGE (For Android WebViews & Mobile Browsers) ───
import uuid
import base64
import time
from io import BytesIO
from flask import send_file

_invoice_pdf_cache = {}

@app.route('/api/upload-invoice-pdf/<order_id>', methods=['POST'])
def api_upload_invoice_pdf(order_id):
    """Client-side generated PDF ko accept karke temporary direct download link generate karta hai (Android WebView compatible)."""
    try:
        data = request.get_json(silent=True) or {}
        pdf_b64 = data.get('pdf_base64', '')
        if not pdf_b64:
            return jsonify({'success': False, 'message': 'No PDF data received'}), 400
        
        if ',' in pdf_b64:
            pdf_b64 = pdf_b64.split(',', 1)[1]
            
        pdf_bytes = base64.b64decode(pdf_b64)
        
        token = str(uuid.uuid4())[:16]
        if len(_invoice_pdf_cache) > 50:
            oldest_key = next(iter(_invoice_pdf_cache))
            _invoice_pdf_cache.pop(oldest_key, None)
            
        short_oid = str(order_id)[-8:].upper()
        filename = f"Jippy_Invoice_{short_oid}.pdf"
        
        _invoice_pdf_cache[token] = {
            'bytes': pdf_bytes,
            'filename': filename,
            'created_at': time.time()
        }
        
        return jsonify({
            'success': True,
            'download_url': f"/api/get-invoice-pdf/{token}",
            'filename': filename
        })
    except Exception as e:
        print(f"❌ Error uploading invoice PDF: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/get-invoice-pdf/<token>')
def api_get_invoice_pdf(token):
    """Direct HTTP attachment download jo Android WebView ke DownloadManager mein 100% download hota hai."""
    entry = _invoice_pdf_cache.get(token)
    if not entry:
        return "Invoice download link expired or not found. Please click 'Download PDF' again.", 404
        
    return send_file(
        BytesIO(entry['bytes']),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=entry.get('filename', 'Jippy_Invoice.pdf')
    )

# ─── SEO SEARCH ENGINE INFRASTRUCTURE (Robots.txt & Sitemap.xml) ───
@app.route('/robots.txt')
def serve_robots_txt():
    """🤖 Search Engine Crawl Directives"""
    from flask import send_from_directory
    import os
    return send_from_directory(os.path.join(app.root_path, 'static'), 'robots.txt', mimetype='text/plain')

@app.route('/sitemap.xml')
def serve_sitemap_xml():
    """🗺️ Dynamic XML Sitemap for Google, Bing and Search Engine Indexing"""
    from flask import Response
    import datetime
    import urllib.parse

    base_url = "https://jippy.store"
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    urls = [
        {"loc": f"{base_url}/", "changefreq": "daily", "priority": "1.0"},
        {"loc": f"{base_url}/shops", "changefreq": "daily", "priority": "0.9"},
        {"loc": f"{base_url}/societies", "changefreq": "weekly", "priority": "0.8"},
        {"loc": f"{base_url}/login", "changefreq": "monthly", "priority": "0.5"},
        {"loc": f"{base_url}/privacy", "changefreq": "monthly", "priority": "0.4"},
    ]

    try:
        cat_list = get_cached_categories() if callable(get_cached_categories) else []
        for cat in cat_list:
            encoded_cat = urllib.parse.quote_plus(str(cat))
            urls.append({
                "loc": f"{base_url}/?category={encoded_cat}",
                "changefreq": "daily",
                "priority": "0.8"
            })
    except Exception as e:
        print("Sitemap categories error:", e)

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
        '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
    ]

    for u in urls:
        xml_lines.append('  <url>')
        xml_lines.append(f'    <loc>{u["loc"]}</loc>')
        xml_lines.append(f'    <lastmod>{now_str}</lastmod>')
        xml_lines.append(f'    <changefreq>{u["changefreq"]}</changefreq>')
        xml_lines.append(f'    <priority>{u["priority"]}</priority>')
        if u["loc"] == f"{base_url}/":
            xml_lines.append('    <image:image>')
            xml_lines.append(f'      <image:loc>{base_url}/static/logo.png</image:loc>')
            xml_lines.append('      <image:title>Jippy Store - 10-15 Mins Hyperlocal Grocery Delivery</image:title>')
            xml_lines.append('    </image:image>')
        xml_lines.append('  </url>')

    xml_lines.append('</urlset>')

    return Response("\n".join(xml_lines), mimetype="application/xml")

@app.route("/admin/all-users")
def admin_all_users():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    # Fetch all users sorted by role and name
    all_users = list(users.find().sort([("role", 1), ("name", 1)]))
    
    # Process data for display (converting ObjectIds to strings)
    for user in all_users:
        user["_id"] = str(user["_id"])
        # Format the creation date if it exists
        if "created_at" in user and user["created_at"]:
            if user["created_at"].tzinfo is None:
                user["created_at"] = pytz.utc.localize(user["created_at"])
            user["display_date"] = user["created_at"].astimezone(IST).strftime('%d %b %Y')
            user["sort_date"] = user["created_at"].astimezone(IST).strftime('%Y%m%d%H%M%S')
        else:
            user["display_date"] = "N/A"
            user["sort_date"] = "00000000000000"

    return render_template("admin_all_users.html", all_users=all_users)

# --- ANDROID NOTIFICATION API ---
@app.route('/admin/api/check-new-orders')
def check_new_orders():
    """
    Android Service ise har 2 minute mein call karegi.
    Sirf un orders ko dhundega jo 'pending' hain aur pichle 150 seconds mein aaye hain.
    """
    try:
        # Piche ke 2.5 minutes ka time nikalna (150 seconds buffer ke liye)
        threshold_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=150)

        # Database query: Status 'pending' aur Time threshold se zyada
        new_order = orders.find_one({
            "created_at": {"$gte": threshold_time},
            "status": "pending"
        })

        if new_order:
            # Android Service 'NEW_ORDER' string ka hi wait kar rahi hai
            return "NEW_ORDER", 200
        else:
            return "IDLE", 200

    except Exception as e:
        print(f"❌ Notification API Error: {e}")
        return "ERROR", 500

@app.route("/admin/societies", methods=["GET", "POST"])
def admin_societies():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    error = None
    success = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()
        lat_raw = request.form.get("lat", "").strip()
        lng_raw = request.form.get("lng", "").strip()
        image = request.form.get("image", "").strip()

        # Comprehensive input validations
        lat = None
        lng = None
        if not name or len(name) < 3:
            error = "Society name is required (minimum 3 characters)."
        elif not city or len(city) < 2:
            error = "City name is required (minimum 2 characters)."
        elif not pincode or not re.match(r"^[1-9][0-9]{5}$", pincode):
            error = "Please enter a valid 6-digit Indian PIN code (e.g. 201301)."
        else:
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                    error = "Coordinates out of bounds (-90 to 90 latitude, -180 to 180 longitude)."
            except (ValueError, TypeError):
                error = "Please enter valid numeric latitude and longitude coordinates."

        # Duplicate check in the same city
        if not error:
            existing = societies.find_one({
                "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
                "city": {"$regex": f"^{re.escape(city)}$", "$options": "i"}
            })
            if existing:
                error = f"A society named '{name}' already exists in '{city}'."

        if not error:
            societies.insert_one({
                "name": name,
                "city": city,
                "pincode": pincode,
                "image": image or None,
                "location": {
                    "lat": lat,
                    "lng": lng
                },
                "created_at": datetime.datetime.now()
            })
            success = f"Society '{name}' in {city} successfully added!"
            socs = list(societies.find().sort("created_at", -1))
            return render_template("admin_societies.html", societies=socs, success=success)
        else:
            socs = list(societies.find().sort("created_at", -1))
            return render_template("admin_societies.html", societies=socs, error=error, form_data=request.form)

    socs = list(societies.find().sort("created_at", -1))
    return render_template("admin_societies.html", societies=socs)

@app.route("/admin/society/edit/<society_id>", methods=["GET", "POST"])
def admin_edit_society(society_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        soc = societies.find_one({"_id": ObjectId(society_id)})
    except Exception:
        soc = None

    if not soc:
        return redirect(url_for("admin_societies"))

    error = None
    success = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()
        lat_raw = request.form.get("lat", "").strip()
        lng_raw = request.form.get("lng", "").strip()
        image = request.form.get("image", "").strip()

        # Comprehensive input validations
        lat = None
        lng = None
        if not name or len(name) < 3:
            error = "Society name is required (minimum 3 characters)."
        elif not city or len(city) < 2:
            error = "City name is required (minimum 2 characters)."
        elif not pincode or not re.match(r"^[1-9][0-9]{5}$", pincode):
            error = "Please enter a valid 6-digit Indian PIN code (e.g. 201301)."
        else:
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                    error = "Coordinates out of bounds (-90 to 90 latitude, -180 to 180 longitude)."
            except (ValueError, TypeError):
                error = "Please enter valid numeric latitude and longitude coordinates."

        # Duplicate check excluding current society
        if not error:
            existing = societies.find_one({
                "_id": {"$ne": ObjectId(society_id)},
                "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
                "city": {"$regex": f"^{re.escape(city)}$", "$options": "i"}
            })
            if existing:
                error = f"Another society named '{name}' already exists in '{city}'."

        if not error:
            societies.update_one(
                {"_id": ObjectId(society_id)},
                {"$set": {
                    "name": name,
                    "city": city,
                    "pincode": pincode,
                    "image": image or None,
                    "location": {
                        "lat": lat,
                        "lng": lng
                    },
                    "updated_at": datetime.datetime.now()
                }}
            )
            soc = societies.find_one({"_id": ObjectId(society_id)})
            success = f"Society '{name}' updated successfully!"
            return render_template("edit_society.html", soc=soc, success=success)
        else:
            temp_soc = dict(soc)
            temp_soc["name"] = name
            temp_soc["city"] = city
            temp_soc["pincode"] = pincode
            temp_soc["image"] = image
            temp_soc["location"] = {"lat": lat_raw, "lng": lng_raw}
            return render_template("edit_society.html", soc=temp_soc, error=error)

    return render_template("edit_society.html", soc=soc)

@app.route("/admin/society/delete/<society_id>", methods=["POST"])
def admin_delete_society(society_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    
    try:
        societies.delete_one({"_id": ObjectId(society_id)})
    except Exception:
        pass
    return redirect(url_for("admin_societies"))

import math

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Radius of the earth in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat/2) * math.sin(dLat/2) +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
         math.sin(dLon/2) * math.sin(dLon/2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)) 
    d = R * c # Distance in km
    return d

@app.route("/api/nearby-societies", methods=["POST"])
def nearby_societies():
    data = request.json
    user_lat = data.get("lat")
    user_lng = data.get("lng")
    
    if user_lat is None or user_lng is None:
        return jsonify({"error": "Location is required"}), 400
        
    all_socs = list(societies.find())
    results = []
    
    for soc in all_socs:
        sloc = soc.get("location", {})
        slat = sloc.get("lat")
        slng = sloc.get("lng")
        
        if slat and slng:
            dist = haversine_distance(float(user_lat), float(user_lng), slat, slng)
            results.append({
                "id": str(soc["_id"]),
                "name": soc.get("name"),
                "city": soc.get("city"),
                "pincode": soc.get("pincode"),
                "image": soc.get("image"),
                "distance": round(dist, 1)
            })
            
    # Sort by distance
    results.sort(key=lambda x: x["distance"])
    # Return top 10
    return jsonify({"societies": results[:10]})

@app.route("/api/search-societies", methods=["GET"])
def search_societies():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"societies": []})
        
    regex_q = {"$regex": query, "$options": "i"}
    socs = list(societies.find({
        "$or": [
            {"city": regex_q},
            {"pincode": regex_q},
            {"name": regex_q}
        ]
    }).limit(20))
    
    results = []
    for soc in socs:
        results.append({
            "id": str(soc["_id"]),
            "name": soc.get("name"),
            "city": soc.get("city"),
            "pincode": soc.get("pincode"),
            "image": soc.get("image")
        })
        
    return jsonify({"societies": results})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 3001))
    app.run(host="0.0.0.0", port=port, debug=True)
