# 🛒 Jippy Store — Hyper-Local Grocery & Quick Commerce Platform

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/framework-Flask%203.1-green.svg)](https://flask.palletsprojects.com/)
[![Database](https://img.shields.io/badge/database-MongoDB%20Atlas-brightgreen.svg)](https://www.mongodb.com/cloud/atlas)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](#license)

**Jippy Store** (also known as FreshKart) is a full-featured, hyper-local grocery e-commerce and quick delivery platform. It connects local vendors, delivery executives, and residential societies with seamless ordering, real-time OTP verification, inventory management, dynamic pricing, and order fulfillment.

---

## 🌟 Key Features

### 🛍️ Customer Experience
- **Hyper-Local Society Selection**: Delivery addresses tailored to supported societies and local service areas.
- **Product Catalog & Search**: Real-time product search, category filtering, unit selection, and stock status indicators.
- **Cart & Dynamic Pricing**: Automatic calculation of cart subtotals, configurable platform fees, and dynamic delivery fees based on order threshold.
- **Order Management & Tracking**: View order history, track order status (Pending, Processing, Out for Delivery, Delivered, Cancelled), and view downloadable/printable invoices.
- **QR Code Verification**: QR code generation for secure delivery handoff verification.

### 🛡️ Admin Command Center
- **Dashboard Analytics**: Overview of total sales, active orders, total registered users, and active societies.
- **Order Operations**: Live order monitoring, status workflow updating, and order cancellations.
- **Product Management**: Full CRUD capability for products, including Cloudinary image uploads, pricing, stock levels, and category tagging.
- **User & Society Management**: Manage customer accounts, block/unblock users, assign user roles, and define delivery service zones/societies.
- **Fees & Settings Configurator**: Live controls for platform fee rates, delivery charge thresholds, and minimum order values.

### 🚚 Vendor & Delivery Executive Portals
- **Delivery Partner Portal**: View assigned orders, inspect delivery addresses, scan/verify QR codes or enter verification codes upon completion.
- **Vendor Inventory Portal**: Monitor product availability, stock levels, and incoming store orders in real-time.

### 🔒 Security & Verification
- **Phone OTP Verification**: Integrated with **Twilio Verify** for SMS OTP-based user authentication.
- **Firebase Authentication**: Integrated **Firebase Admin SDK** for secure token verification and multi-factor capabilities.
- **Password Hashing**: Secure password hashing with **Flask-Bcrypt**.
- **Role-Based Access Control (RBAC)**: Session guards protecting admin and portal routes.

---

## 🛠️ Tech Stack

| Component | Technology / Service |
| :--- | :--- |
| **Backend Framework** | [Flask 3.1](https://flask.palletsprojects.com/) (Python) |
| **Database** | [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) (PyMongo) |
| **Authentication** | Firebase Admin SDK, Twilio Verify (SMS OTP), Flask-Bcrypt |
| **Media Hosting** | [Cloudinary](https://cloudinary.com/) API (Product Image Storage) |
| **Caching** | Flask-Caching (In-Memory SimpleCache) |
| **Media & PDF Utilities** | ReportLab, PyPDF, qrcode, Pillow |
| **Frontend** | Jinja2 Templates, Vanilla HTML5, CSS3, JavaScript |

---

## 📂 Project Structure

```
Jippy Store/
├── app.py                                            # Main Flask application & route handlers
├── requirements.txt                                  # Python dependencies list
├── .env                                              # Environment configuration (secrets & API keys)
├── jippy-936b9-firebase-adminsdk-fbsvc-926fa22dd5.json  # Firebase Admin SDK Credentials
├── static/                                           # Static assets
│   ├── images/                                       # App graphics and product media
│   ├── sounds/                                       # Notification alert sounds
│   └── logo.jpeg                                     # Store logo
└── templates/                                        # HTML Jinja2 Templates
    ├── base.html                                     # Base customer store layout
    ├── base_admin.html                               # Base admin command center layout
    ├── base_portal.html                             # Base delivery/vendor portal layout
    ├── products.html                                 # Product catalog view
    ├── cart.html                                     # Shopping cart & checkout
    ├── my_orders.html                                # Customer order history
    ├── admin_dashboard.html                          # Admin dashboard
    ├── admin_orders.html                             # Admin order management
    ├── portal_delivery.html                          # Delivery partner interface
    ├── portal_vendor.html                            # Vendor inventory manager
    └── ... (additional views & partials)
```

---

## 🚀 Quick Start & Installation

### Prerequisites
- **Python 3.9+** installed on your machine
- **MongoDB Atlas** cluster (or local MongoDB instance)
- **Twilio Account** (for SMS OTP verification)
- **Cloudinary Account** (for product image hosting)

### 1. Clone the Repository
```bash
git clone https://github.com/your-username/jippy-store.git
cd "Jippy Store"
```

### 2. Set Up Virtual Environment
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the root directory (or copy from existing configuration):

```env
# MongoDB Connection
MONGO_URI=mongodb+srv://<username>:<password>@cluster.mongodb.net/grocery_app?retryWrites=true&w=majority

# Flask Secret Key
SECRET_KEY=your_super_secret_flask_key

# Admin Credentials
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your_admin_password

# Cloudinary Setup
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret

# Twilio Verify Setup
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+10000000000
TWILIO_VERIFY_SERVICE_SID=your_verify_service_sid
TWILIO_PROXY_NUMBER=+10000000000

# App Settings & Fee Configurations
PLATFORM_FEE=2
BASE_URL=http://localhost:5000
```

### 5. Firebase Credentials
Ensure your Firebase Admin SDK service account key JSON (`jippy-936b9-firebase-adminsdk-fbsvc-926fa22dd5.json` or as configured in `app.py`) is placed in the project root.

### 6. Run the Application
```bash
python app.py
```
The server will start at `http://localhost:5000` (or `http://127.0.0.1:5000`).

---

## 🔑 Application Access Points

- **Customer Storefront**: `http://localhost:5000/`
- **Admin Dashboard**: `http://localhost:5000/admin/login`
- **Delivery Partner Portal**: `http://localhost:5000/portal/login`
- **Vendor Portal**: `http://localhost:5000/portal/vendor`

---

## 📜 License

This project is open-source and available under the [MIT License](LICENSE).
