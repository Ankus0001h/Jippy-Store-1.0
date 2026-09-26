import os
import re
import random
import datetime
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

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "devkey")

# Cache configuration for performance
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300
cache = Cache(app)

# NEW: long‑lived sessions (e.g. 90 days)
app.permanent_session_lifetime = timedelta(days=90)

# ---------- Security & admin creds ----------
bcrypt = Bcrypt(app)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# ---------- Twilio Verify ----------
account_sid = os.environ["TWILIO_ACCOUNT_SID"]
auth_token = os.environ["TWILIO_AUTH_TOKEN"]
verify_sid = os.environ["TWILIO_VERIFY_SERVICE_SID"]
twilio_client = TwilioClient(account_sid, auth_token)  # Twilio client[web:481]
TW_PROXY_NUM = os.getenv("TWILIO_PROXY_NUMBER")
BASE_URL = os.getenv("BASE_URL", "")

# ---------- MongoDB ----------
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["grocery_app"]

# ---------- Firebase Admin ----------
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
import os

firebase_cred_path = os.path.join(os.path.dirname(__file__), 'jippy-936b9-firebase-adminsdk-fbsvc-926fa22dd5.json')
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(firebase_cred_path)
        firebase_admin.initialize_app(cred)
except Exception as e:
    print(f"Error initializing Firebase Admin: {e}")

# ---------- Fees & thresholds ----------

users = db["users"]
products = db["products"]
orders = db["orders"]
settings = db["settings"]
societies = db["societies"]


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

@app.after_request
def add_cache_headers(response):
    """Adds Cache-Control headers to speed up WebView apps and browsers."""
    if request.path.startswith('/static/'):
        # Cache static files (CSS, JS, images) for 30 days
        response.cache_control.max_age = 2592000
        response.cache_control.public = True
    elif request.path == '/' or request.path.startswith('/category/'):
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
    return dict(current_user=current_user)

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

@cache.memoize(timeout=600)
def get_cached_categories():
    try:
        return sorted([c for c in products.distinct("category") if c])
    except:
        return []

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

@app.route("/category/<path:name>")
def category_page(name):
    try:
        # Step 1: Clean the name
        original_category = clean_cat_name(name)
        
        # Step 2: Database Query (Cached)
        items = get_cached_category_items(original_category)
        
        items = list(items)  # Copy to avoid mutating the cached list
        random.shuffle(items)
        
        # Categories fetch (Cached)
        categories = get_cached_categories()
        
        # Fetch Shop
        shop = None
        shop_id = session.get("current_shop_id")
        if shop_id:
            shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})

        print(f"✅ VERCEL FIX: Category='{original_category}', Found {len(items)} items")

        return render_template("products.html",
                             page_type="products",
                             categories=categories,
                             items=items,
                             selected_category=original_category,
                             item_count=len(items),
                             shop=shop)
    except Exception as e:
        print(f"❌ VERCEL Error: {str(e)}")
        return render_template("products.html", items=[], categories=[], item_count=0, shop=None)

@app.route("/")
def index():
    # Show the location fetcher page
    return render_template("location.html")

@app.route("/set-location", methods=["POST"])
def set_location():
    data = request.json
    session["user_lat"] = data.get("lat")
    session["user_lng"] = data.get("lng")
    session["user_address"] = data.get("address")
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
                "distance": None
            })
        top_societies = results
        user_address = f"Search: {query}"
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
                    "distance": round(dist, 1)
                })
                
        # Sort by distance
        results.sort(key=lambda x: x["distance"])
        top_societies = results[:10]
    
    return render_template("societies.html", societies=top_societies, user_address=user_address)

@app.route("/society/<society_id>/shops")
def society_shops(society_id):
    # Find society
    society = societies.find_one({"_id": ObjectId(society_id)})
    if not society:
        return "Society not found", 404
        
    # Find vendors that serve this society
    # Vendors should have society_id string in their serviceable_societies list
    shops = list(users.find({
        "role": "vendor",
        "serviceable_societies": str(society_id)
    }))
    
    return render_template("shops.html", society=society, shops=shops)

@app.route("/shop/<shop_id>")
def shop_home(shop_id):
    try:
        # Check if shop exists
        shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        if not shop:
            return "Shop not found", 404
            
        session["current_shop_id"] = shop_id
        session["current_shop_name"] = shop.get("shop_name", "Local Shop")
        
        categories = get_cached_categories()
        return render_template("products.html", page_type="categories", categories=categories, shop=shop)
    except Exception as e:
        return render_template("products.html", page_type="categories", categories=[], shop=None)
    
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
        for soc in socs:
            results.append({"id": str(soc["_id"]), "name": soc.get("name"), "city": soc.get("city"), "pincode": soc.get("pincode")})
    return jsonify({"status": "success", "societies": results})

@app.route("/api/society/<society_id>/shops")
def api_society_shops(society_id):
    shops_data = list(users.find({"role": "vendor", "serviceable_societies": str(society_id)}))
    results = []
    for s in shops_data:
        results.append({
            "id": str(s["_id"]),
            "shop_name": s.get("shop_name", "Shop"),
            "owner_name": s.get("name", ""),
            "contact": s.get("phone", "")
        })
    return jsonify({"status": "success", "shops": results})

@app.route("/api/shop/<shop_id>/categories")
def api_shop_categories(shop_id):
    shop = users.find_one({"_id": ObjectId(shop_id), "role": "vendor"})
    if not shop:
        return jsonify({"status": "error", "message": "Shop not found"}), 404
    categories = get_cached_categories()
    cats_data = [{"id": str(c["_id"]), "name": c.get("name"), "image": c.get("image", "")} for c in categories]
    return jsonify({"status": "success", "shop_name": shop.get("shop_name"), "categories": cats_data})
    
# ================================
# ADMIN: AUTH & DASHBOARD
# ================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
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
        session["admin_logged_in"] = True
        return redirect(url_for("admin_dashboard"))

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
    return redirect(url_for("admin_login"))


# ================================
# ADMIN: ADD / EDIT PRODUCT
# ================================

@app.route("/admin/add-product", methods=["GET", "POST"])
def add_product():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        price = request.form.get("price", "").strip()
        discount_price = request.form.get("discount_price", "").strip()  # NEW
        unit = request.form.get("unit", "").strip()
        category = request.form.get("category", "").strip()
        other_category = request.form.get("other_category", "").strip()

        if not all([name, price, unit, category]):
            products_list = list(products.find())
            return render_template(
                "add_product.html",
                error="All fields are required.",
                products=products_list,
            )

        if category == "Other":
            if not other_category:
                products_list = list(products.find())
                return render_template(
                    "add_product.html",
                    error="Please enter a custom category name.",
                    products=products_list,
                )

            # new custom category normalized
            new_cat_norm = normalize_category_name(other_category)

            # existing categories -> normalized map
            existing_cats = products.distinct("category")
            existing_map = {
                normalize_category_name(c): c
                for c in existing_cats
                if isinstance(c, str)
            }

            if new_cat_norm in existing_map:
                # same category (case/space ignore) already exists
                category = existing_map[new_cat_norm]
            else:
                # completely new category
                category = other_category.strip()

        slug = slugify(name + "-" + unit)

        if "image" not in request.files:
            products_list = list(products.find())
            return render_template(
                "add_product.html",
                error="No image file uploaded.",
                products=products_list,
            )

        file = request.files["image"]
        if file.filename == "":
            products_list = list(products.find())
            return render_template(
                "add_product.html",
                error="No file selected.",
                products=products_list,
            )

        upload_result = cloudinary.uploader.upload(file, folder="products")

        # base document
        doc = {
            "name": name,
            "slug": slug,
            "price": float(price),
            "unit": unit,
            "category": category,
            "image": upload_result["secure_url"],
            "image_public_id": upload_result["public_id"],
        }

        # optional discounted price
        if discount_price:
            try:
                doc["discount_price"] = float(discount_price)
            except ValueError:
                # agar admin ne galat value dali to error dikha do
                products_list = list(products.find())
                return render_template(
                    "add_product.html",
                    error="Please enter a valid discounted price.",
                    products=products_list,
                )

        products.insert_one(doc)
        return redirect(url_for("add_product"))

    products_list = list(products.find())
    return render_template("add_product.html", products=products_list)

@app.route("/admin/delete-product/<product_id>", methods=["POST"])
def delete_product(product_id):
    if not session.get("admin_logged_in"):
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
    except Exception:
        pass

    return redirect(url_for("add_product"))


@app.route("/admin/edit-product/<product_id>", methods=["GET", "POST"])
def edit_product(product_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    product = products.find_one({"_id": ObjectId(product_id)})
    if not product:
        return redirect(url_for("add_product"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        price = request.form.get("price", "").strip()
        discount_price = request.form.get("discount_price", "").strip()  # NEW
        unit = request.form.get("unit", "").strip()
        category = request.form.get("category", "").strip()
        other_category = request.form.get("other_category", "").strip()

        if not all([name, price, unit, category]):
            return render_template(
                "edit_product.html",
                error="All fields are required.",
                product=product,
            )

        if category == "Other":
            if not other_category:
                return render_template(
                    "edit_product.html",
                    error="Please enter a custom category name.",
                    product=product,
                )
            category = other_category

        # base update document
        update_doc = {
            "name": name,
            "price": float(price),
            "unit": unit,
            "category": category,
        }

        # optional discounted price handling
        if discount_price:
            try:
                update_doc["discount_price"] = float(discount_price)
            except ValueError:
                return render_template(
                    "edit_product.html",
                    error="Please enter a valid discounted price.",
                    product=product,
                )
        else:
            # agar blank hai to field ko unset kar do (remove discount)
            update_doc = {"$unset": {"discount_price": ""}}

        # image update (optional)
        if "image" in request.files:
            file = request.files["image"]
            if file and file.filename:
                # delete old image if exists
                old_public_id = product.get("image_public_id")
                if old_public_id:
                    try:
                        cloudinary.uploader.destroy(old_public_id, invalidate=True)
                    except:
                        pass
                
                upload_result = cloudinary.uploader.upload(file, folder="products")
                update_doc["image"] = upload_result["secure_url"]
                update_doc["image_public_id"] = upload_result["public_id"]

        # proper MongoDB update structure
        if "discount_price" not in update_doc and "$unset" not in update_doc:
            # only $set if no discount changes
            products.update_one(
                {"_id": ObjectId(product_id)}, 
                {"$set": update_doc}
            )
        else:
            # handle $set and $unset together
            set_update = {k: v for k, v in update_doc.items() if k != "$unset"}
            unset_update = update_doc.get("$unset", {})
            
            update_payload = {}
            if set_update:
                update_payload["$set"] = set_update
            if unset_update:
                update_payload["$unset"] = unset_update
            
            products.update_one(
                {"_id": ObjectId(product_id)}, 
                update_payload
            )

        return redirect(url_for("add_product"))

    return render_template("edit_product.html", product=product)

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
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        vendor_type = request.form.get("vendor_type", "").strip()
        vendor_type_other = request.form.get("vendor_type_other", "").strip()
        
        shop_name = request.form.get("shop_name", "").strip()
        shop_image = request.form.get("shop_image", "").strip()
        serviceable_societies = request.form.getlist("serviceable_societies")

        # NEW: delivery phone (10 digit) -> normalize to +91
        raw_phone = request.form.get("phone", "").strip()
        phone = None
        if raw_phone:
            # sirf digits lo
            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) == 10:
                phone = "+91" + digits
            elif digits.startswith("91") and len(digits) == 12:
                phone = "+" + digits
            else:
                # galat format -> error
                error = "Please enter a valid 10-digit phone for delivery user"
                vendors = list(users.find({"role": "vendor"}))
                deliveries = list(users.find({"role": "delivery"}))
                all_societies = list(societies.find())
                return render_template(
                    "add_user.html",
                    error=error,
                    vendors=vendors,
                    deliveries=deliveries,
                    all_societies=all_societies,
                )

        # lat / lng from onboarding form
        raw_lat = request.form.get("lat", "").strip()
        raw_lng = request.form.get("lng", "").strip()
        try:
            lat = float(raw_lat) if raw_lat else None
            lng = float(raw_lng) if raw_lng else None
        except ValueError:
            lat = None
            lng = None

        # Basic validations
        if role not in ["vendor", "delivery"]:
            error = "Invalid role"
        elif not name or not email or not password:
            error = "Name, email and password are required"
        elif role == "delivery" and not phone:
            error = "Phone is required for delivery users"
        else:
            error = None

        if error:
            vendors = list(users.find({"role": "vendor"}))
            deliveries = list(users.find({"role": "delivery"}))
            all_societies = list(societies.find())
            return render_template(
                "add_user.html",
                error=error,
                vendors=vendors,
                deliveries=deliveries,
                all_societies=all_societies,
            )

        existing = users.find_one({"email": email, "role": role})
        if existing:
            vendors = list(users.find({"role": "vendor"}))
            deliveries = list(users.find({"role": "delivery"}))
            all_societies = list(societies.find())
            return render_template(
                "add_user.html",
                error="User with this email and role already exists",
                vendors=vendors,
                deliveries=deliveries,
                all_societies=all_societies,
            )

        # Final vendor_type
        final_vendor_type = None
        if role == "vendor":
            if vendor_type == "other":
                final_vendor_type = vendor_type_other.strip() or None
            else:
                final_vendor_type = vendor_type or None

        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        doc = {
            "name": name,
            "email": email,
            "password": pw_hash,
            "role": role,
        }

        if final_vendor_type:
            doc["vendor_type"] = final_vendor_type

        # NEW: phone sirf delivery users ke liye store
        if role == "delivery" and phone:
            doc["phone"] = phone

        # coords field (optional)
        if lat is not None and lng is not None:
            doc["coords"] = {"lat": lat, "lng": lng}

        if role == "vendor":
            doc["shop_name"] = shop_name
            doc["shop_image"] = shop_image
            doc["serviceable_societies"] = serviceable_societies

        users.insert_one(doc)

        vendors = list(users.find({"role": "vendor"}))
        deliveries = list(users.find({"role": "delivery"}))
        all_societies = list(societies.find())
        return render_template(
            "add_user.html",
            success="User created successfully",
            vendors=vendors,
            deliveries=deliveries,
            all_societies=all_societies,
        )

    # GET: show form + existing users
    vendors = list(users.find({"role": "vendor"}))
    deliveries = list(users.find({"role": "delivery"}))
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
        email = request.form.get("email", "").strip()
        role = request.form.get("role", "").strip()
        vendor_type = request.form.get("vendor_type", "").strip()
        vendor_type_other = request.form.get("vendor_type_other", "").strip()

        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # NEW: phone edit (10-digit -> +91)
        raw_phone = request.form.get("phone", "").strip()
        phone = None
        if raw_phone:
            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) == 10:
                phone = "+91" + digits
            elif digits.startswith("91") and len(digits) == 12:
                phone = "+" + digits
            else:
                error = "Please enter a valid 10-digit phone for delivery user"
                all_societies = list(societies.find())
                return render_template("edit_user.html", error=error, user=user, all_societies=all_societies)

        # lat / lng from edit form (same field names)
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

        if role not in ["vendor", "delivery"]:
            error = "Invalid role"
        elif not name or not email:
            error = "Name and email are required"
        elif new_password and new_password != confirm_password:
            error = "New password and confirm password do not match"
        elif role == "delivery" and not (phone or user.get("phone")):
            # agar pehle bhi phone nahi tha aur ab bhi blank hai
            error = "Phone is required for delivery users"
        else:
            error = None

        if error:
            all_societies = list(societies.find())
            return render_template("edit_user.html", error=error, user=user, all_societies=all_societies)

        final_vendor_type = None
        if role == "vendor":
            if vendor_type == "other":
                final_vendor_type = vendor_type_other.strip() or None
            else:
                final_vendor_type = vendor_type or None

        update_doc = {
            "name": name,
            "email": email,
            "role": role,
        }

        if final_vendor_type:
            update_doc["vendor_type"] = final_vendor_type
        else:
            update_doc["vendor_type"] = None

        # NEW: phone update logic
        if role == "delivery":
            if phone:
                update_doc["phone"] = phone
        else:
            # vendor ke liye phone clear kar sakte ho ya rehne do; yahan clear kar diya
            update_doc["phone"] = None

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
# VENDOR / DELIVERY PORTAL (unchanged)
# ================================

@app.route("/portal/login", methods=["GET", "POST"])
def portal_login():
    # GET: captcha generate + show
    if request.method == "GET":
        code = "".join(str(random.randint(0, 9)) for _ in range(4))
        session["portal_captcha_code"] = code
        return render_template("portal_login.html", captcha_code=code)

    # POST: validate email/password + captcha
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
            "portal_login.html",
            error="Invalid captcha. Please try again.",
            captcha_code=code,
        )

    # Sirf vendor/delivery users ke liye lookup
    doc = users.find_one({
        "email": email,
        "role": {"$in": ["vendor", "delivery"]},
    })

    stored_hash = doc.get("password") if doc else None

    if stored_hash and bcrypt.check_password_hash(stored_hash, password):
        # session fields for portal
        session["user_role"] = doc.get("role")
        session["user_email"] = doc.get("email")
        session["portal_user_id"] = str(doc.get("_id"))
        session["portal_user_name"] = doc.get("name") or doc.get("email")
        return redirect(url_for("portal_dashboard"))

    # Wrong credentials -> new captcha
    code = "".join(str(random.randint(0, 9)) for _ in range(4))
    session["portal_captcha_code"] = code
    return render_template(
        "portal_login.html",
        error="Invalid credentials.",
        captcha_code=code,
    )

@app.route("/portal/logout")
def portal_logout():
    session.pop("user_role", None)
    session.pop("user_email", None)
    return redirect(url_for("portal_login"))


from bson import ObjectId
import datetime

@app.route("/portal/dashboard")
def portal_dashboard():
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    user_name = session.get("portal_user_name", "Partner")

    if not role or not portal_user_id:
        return redirect(url_for("portal_login"))

    vendor_orders_active = []
    vendor_orders_history = []
    delivery_orders_active = []
    delivery_orders_history = []

    if role == "vendor":
        vendor_oid = ObjectId(portal_user_id)
        all_vendor_orders = list(
            orders.find({"items.vendor_id": vendor_oid}).sort("created_at", -1)
        )

        for o in all_vendor_orders:
            vstatus = o.get("vendor_status") or "pending"
            if vstatus == "ready":
                vendor_orders_history.append(o)
            else:
                vendor_orders_active.append(o)

    if role == "delivery":
        delivery_oid = ObjectId(portal_user_id)
        all_delivery_orders = list(
            orders.find({"delivery_id": delivery_oid}).sort("created_at", -1)
        )
        
        for o in all_delivery_orders:
            dstatus = (o.get("delivery_status") or "").lower().strip()
            
            # 🔥 FIXED: Perfect items processing - RELAXED filter for real data
            items_raw = o.get("items", [])
            if items_raw:
                if not isinstance(items_raw, list):
                    items_raw = list(items_raw)
                
                # ✅ FIXED FILTER: name + price + qty chahiye, unit optional
                o["items"] = [
                    it for it in items_raw 
                    if (
                        it.get("name") and                                    # Name ✅
                        isinstance(it.get("price", 0), (int, float)) and      # Valid price ✅
                        (float(it.get("price", 0)) or 0) > 0 and              # Price > 0 ✅
                        isinstance(it.get("qty", 0), (int, float)) and        # Valid qty ✅
                        int(it.get("qty", 0)) > 0                             # Qty > 0 ✅
                        # unit optional banaya - agar nahi hai to bhi show ho jayega
                    )
                ]
                
                # ✅ Detailed debug (remove in production)
               
                if o['items']:
                    sample_item = o['items'][0]
                    print(f"   Sample keys: {list(sample_item.keys())}")
                    print(f"   Sample data: {sample_item}")
                
            else:
                o["items"] = []
            
            # delivered ho chuka hai to history me bhej
            if dstatus == "delivered":
                delivery_orders_history.append(o)
            else:
                delivery_orders_active.append(o)

    current_date = datetime.datetime.now(datetime.timezone.utc)

    return render_template(
        "portal_dashboard.html",
        role=role,
        user_name=user_name,
        current_date=current_date,
        vendor_orders=vendor_orders_active,
        vendor_orders_history=vendor_orders_history,
        delivery_orders=delivery_orders_active,  # ✅ Now shows ALL valid items!
        delivery_orders_history=delivery_orders_history,
    )

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
    id_instance = "710722692452"
    api_token = "c3a48ad406db49e9bb4b5215868c36cad110862d546f4b38a1"
    url = f"https://api.green-api.com/waInstance{id_instance}/sendMessage/{api_token}"
    
    # Ensure phone has country code and no '+'. Indian numbers default to 91.
    phone = phone.lstrip("+")
    if len(phone) == 10:
        phone = "91" + phone
        
    payload = {
        "chatId": f"{phone}@c.us",
        "message": message
    }
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.status_code == 200
    except Exception as e:
        print("Green API Error:", e)
        return False

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
    
    msg = f"Welcome to Jippy Store!\n\nYour verification code is: *{otp_code}*\n\nPlease do not share this code with anyone."
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

    address = data.get("address")
    if not address:
        return jsonify({"error": "Address is required"}), 400

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

    # ---- STEP 3: Order object creation ----
    order = {
        "user_id": user_id,
        "order_id": order_id,
        "user_phone": user_phone,
        "user_name": user_name,
        "items": cart,
        "vendor_status": vendor_status,
        "subtotal": subtotal,
        "platform_fee": platform_fee,
        "delivery_fee": delivery_fee,
        "handling_fee": handling_fee, # Saved in order
        "surge_charge": surge_charge, # Saved in order
        "total": grand_total,
        "status": "pending",
        "payment_method": "COD",
        "address": {
            "alt_phone": address.get("alt_phone"),
            "line1": address.get("line1", ""),
            "line2": address.get("line2", ""),
            "city": address.get("city", ""),
            "pincode": address.get("pincode", ""),
            "location_text": address.get("location_text", ""),
        },
        "coords": {"lat": lat, "lng": lng} if (lat is not None and lng is not None) else None,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }

    orders.insert_one(order)

    return jsonify({
        "message": "Order placed successfully",
        "order_id": order_id,
        "total": grand_total,
    })

@app.route("/cart")
def cart_page():
    is_logged_in = bool(session.get("user_id"))

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
        surge_charge=surge_charge   # Passed to HTML
    )
    
@app.route("/address", methods=["GET"])
def address_page():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    user_doc = users.find_one({"_id": ObjectId(user_id), "role": "customer"})
    saved_addresses = user_doc.get("addresses", []) if user_doc else []

    return render_template("address.html", saved_addresses=saved_addresses)


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

    # YAHAN users collection se vendors/delivery nikaalna hai
    all_vendors = list(users.find({"role": "vendor"}, {"_id": 1, "name": 1}))
    all_delivery = list(users.find({"role": "delivery"}, {"_id": 1, "name": 1}))

    return render_template(
        "admin_orders.html",
        orders=all_orders,
        vendors=all_vendors,
        delivery_boys=all_delivery,
    )


@app.route("/admin/order/<order_id>/assign-vendor", methods=["POST"])
def admin_assign_vendor(order_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    vendor_id = request.form.get("vendor_id") or None
    update = {"$set": {"vendor_id": ObjectId(vendor_id) if vendor_id else None}}
    orders.update_one({"order_id": order_id}, update)
    return redirect(url_for("admin_orders"))


@app.route("/admin/order/<order_id>/assign-delivery", methods=["POST"])
def admin_assign_delivery(order_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    delivery_id = request.form.get("delivery_id") or None
    update = {"$set": {"delivery_id": ObjectId(delivery_id) if delivery_id else None}}
    orders.update_one({"order_id": order_id}, update)
    return redirect(url_for("admin_orders"))

@app.route("/admin/order/<order_id>/status", methods=["POST"])
def admin_update_status(order_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    # Allowed statuses (enums) jo templates/stepper use karenge
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
        # agar DELIVERED mark kar rahe ho to delivery time bhi save karo
        if new_status == "DELIVERED":
            update["delivered_at"] = datetime.datetime.now(datetime.timezone.utc)

        orders.update_one(
            {"order_id": order_id},
            {"$set": update},
        )

    return redirect(url_for("admin_orders"))


@app.route("/admin/order/<order_id>/delete", methods=["POST"])
def admin_delete_order(order_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    orders.delete_one({"order_id": order_id})
    return redirect(url_for("admin_orders"))

from bson import ObjectId

@app.route("/admin/order/<order_id>/assign-vendor-items", methods=["POST"])
def admin_assign_vendor_items(order_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    # Order fetch karo
    order = orders.find_one({"order_id": order_id})
    if not order:
        return redirect(url_for("admin_orders"))

    items = order.get("items", [])
    if not isinstance(items, list):
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
                # agar ObjectId parse na ho to ignore karo
                item.pop("vendor_id", None)
        else:
            # empty select -> vendor remove
            item.pop("vendor_id", None)

        updated_items.append(item)

    # Order ka items array update karo
    orders.update_one(
        {"order_id": order_id},
        {"$set": {"items": updated_items}}
    )

    # Optional: overall vendor_status set karo (agar kam se kam 1 item assign hai)
    has_any_vendor = any(it.get("vendor_id") for it in updated_items)
    orders.update_one(
        {"order_id": order_id},
        {"$set": {"vendor_status": "assigned" if has_any_vendor else "pending"}}
    )

    return redirect(url_for("admin_orders"))

@app.route("/portal/order/<order_id>/vendor-status", methods=["POST"])
def portal_update_vendor_status(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")

    # sirf vendor ko allow
    if role != "vendor" or not portal_user_id:
        return redirect(url_for("portal_login"))

    new_status = request.form.get("vendor_status", "").strip()
    allowed = {"received", "processing", "packed", "ready", "out_for_delivery", "delivered"}

    if new_status in allowed:
        update_fields = {"vendor_status": new_status, "status": new_status}
        if new_status == "delivered":
            update_fields["delivered_at"] = datetime.datetime.now(datetime.timezone.utc)
            
        orders.update_one(
            {"order_id": order_id},
            {"$set": update_fields}
        )

    return redirect(url_for("portal_dashboard"))

@app.route("/portal/order/<order_id>/delivery-status", methods=["POST"])
def portal_update_delivery_status(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")

    # sirf delivery role allow
    if role != "delivery" or not portal_user_id:
        return redirect(url_for("portal_login"))

    new_status = request.form.get("delivery_status", "").strip()
    allowed = {"accepted", "picked", "out_for_delivery", "delivered"}

    if new_status in allowed:
        orders.update_one(
            {
                "order_id": order_id,
                "delivery_id": ObjectId(portal_user_id),
            },
            {"$set": {"delivery_status": new_status, "status": new_status}},
        )

    return redirect(url_for("portal_dashboard"))

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
        return redirect(url_for("portal_login"))

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
    return render_template("portal_inventory.html", products=items)

@app.route("/admin/inventory/toggle/<product_id>", methods=["POST"])
def portal_inventory_toggle(product_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    next_status = request.form.get("next_status")
    if next_status not in ("oos", "instock"):
        return redirect(url_for("admin_inventory"))

    out_of_stock = (next_status == "oos")
    try:
        products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {"out_of_stock": out_of_stock}}
        )
    except Exception:
        pass

    return redirect(url_for("admin_inventory"))

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

    return render_template(
        "my_profile.html",
        user=user_doc,
        saved_addresses=saved_addresses,
        orders_count=orders_count,
    )

@app.route("/portal/delivery/<order_id>/qr")
def delivery_qr(order_id):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "delivery" or not portal_user_id:
        return redirect(url_for("portal_login"))

    order = orders.find_one({"order_id": order_id})
    if not order:
        return redirect(url_for("portal_dashboard"))

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

@app.errorhandler(Exception) # Yeh line har tarah ke error ko catch karegi
def handle_exception(e):
    import traceback
    with open("crash.log", "w") as f:
        traceback.print_exc(file=f)
    # 1. Agar error standard HTTP error hai (jaise 404, 403, 500)
    if hasattr(e, 'code'):
        code = e.code
    else:
        # 2. Agar koi python code crash hua hai (jaise Database failure ya Bug)
        code = 500
    
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

from flask import jsonify # Ise top par import karein

@app.route("/api/suggestions")
def search_suggestions():
    q = request.args.get('q', '').strip()
    if not q: return jsonify([]) # Khali input par kuch nahi dikhayenge
    
    # 1. Search in Categories (Matching start of string for better single-char results)
    cat_matches = products.distinct("category", {"category": {"$regex": f"^{q}", "$options": "i"}})
    # Agar start se nahi milta toh kahin bhi dhundhein
    if not cat_matches:
        cat_matches = products.distinct("category", {"category": {"$regex": q, "$options": "i"}})
        
    suggestions = [{"name": c, "type": "category", "url": f"/category/{urllib.parse.quote(c)}"} for c in cat_matches]
    
    # 2. Search in Products (Limit to 15 for speed)
    prod_matches = list(products.find(
        {"name": {"$regex": q, "$options": "i"}},
        {"name": 1, "category": 1}
    ).limit(15))
    
    for p in prod_matches:
        suggestions.append({
            "name": p['name'],
            "type": "product",
            "url": f"/category/{urllib.parse.quote(p['category'])}"
        })
        
    return jsonify(suggestions[:20])
   
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
            return "Invoice is only available for delivered orders.", 403

        # 3. Invoice template render karein
        # Note: 'invoice.html' wahi file hai jo maine pichle message mein di thi
        return render_template("invoice.html", order=order)

    except Exception as e:
        print(f"❌ Invoice Error: {e}")
        return "Internal Server Error", 500

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
        else:
            user["display_date"] = "N/A"

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

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()
        lat = request.form.get("lat", "").strip()
        lng = request.form.get("lng", "").strip()
        image = request.form.get("image", "").strip()
        
        if all([name, city, pincode, lat, lng]):
            societies.insert_one({
                "name": name,
                "city": city,
                "pincode": pincode,
                "image": image,
                "location": {
                    "lat": float(lat),
                    "lng": float(lng)
                },
                "created_at": datetime.datetime.now()
            })
            return redirect(url_for("admin_societies"))
            
    socs = list(societies.find().sort("created_at", -1))
    return render_template("admin_societies.html", societies=socs)

@app.route("/admin/society/edit/<society_id>", methods=["GET", "POST"])
def admin_edit_society(society_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    soc = societies.find_one({"_id": ObjectId(society_id)})
    if not soc:
        return redirect(url_for("admin_societies"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        city = request.form.get("city", "").strip()
        pincode = request.form.get("pincode", "").strip()
        lat = request.form.get("lat", "").strip()
        lng = request.form.get("lng", "").strip()
        image = request.form.get("image", "").strip()

        if all([name, city, pincode, lat, lng]):
            societies.update_one(
                {"_id": ObjectId(society_id)},
                {"$set": {
                    "name": name,
                    "city": city,
                    "pincode": pincode,
                    "image": image,
                    "location": {
                        "lat": float(lat),
                        "lng": float(lng)
                    }
                }}
            )
            return redirect(url_for("admin_societies"))

    return render_template("edit_society.html", soc=soc)

@app.route("/admin/society/delete/<society_id>", methods=["POST"])
def admin_delete_society(society_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    
    societies.delete_one({"_id": ObjectId(society_id)})
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
            "pincode": soc.get("pincode")
        })
        
    return jsonify({"societies": results})

if __name__ == "__main__":
    app.run(debug=True)
