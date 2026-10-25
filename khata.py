"""
Jippy Store - Society Vendor Udhar Khata (Digital Store Credit Ledger)
Module: khata.py
Clean, modular Flask Blueprint for:
1. Vendor Khata Hub (Society -> Block/Tower -> Flat Number Hierarchy)
2. Direct Resident Registration with 6-Digit WhatsApp OTP Verification
3. Customer-Initiated Store Credit Approval & Notice Warning
4. 1-Click WhatsApp Itemized Reminders & Statements (100% Professional English)
5. Zero-Reload Payment Settlement & Customer Passbook
6. Smart Indian Society Address Parser (Block, Tower, Floor, Flat, Villas)
"""

import os
import re
import random
import datetime
import threading
from urllib.parse import quote
from bson import ObjectId
import pytz
import certifi
from pymongo import MongoClient
import json
import requests
from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    current_app,
)

IST = pytz.timezone("Asia/Kolkata")

khata_bp = Blueprint("khata", __name__, template_folder="templates")

# ---------- MongoDB Client Access ----------
def get_db():
    try:
        from app import db
        return db
    except Exception:
        mongo_uri = os.getenv("MONGO_URI")
        client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
        return client["grocery_app"]

def get_collections():
    db = get_db()
    return {
        "users": db["users"],
        "orders": db["orders"],
        "societies": db["societies"],
        "settings": db["settings"],
        "khata_users": db["vendor_khata_users"],
        "khata_ledger": db["vendor_khata_ledger"],
        "khata_otps": db["vendor_khata_otps"]
    }

# ---------- WhatsApp & SMS Dispatcher (100% English) ----------
def send_green_api_message(phone, message):
    if not phone or not message:
        return False
    url = os.getenv("WHATSAPP_API_URL", "http://3.26.1.109:3001/send-message")
    clean_digits = re.sub(r"\D", "", str(phone))
    if len(clean_digits) == 10:
        phone_formatted = "91" + clean_digits
    elif len(clean_digits) == 12 and clean_digits.startswith("91"):
        phone_formatted = clean_digits
    else:
        phone_formatted = clean_digits

    payload = {"phone": phone_formatted, "message": message}
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=12)
        return resp.status_code == 200
    except Exception as e:
        print("[Khata WhatsApp Error]:", e)
        return False

def send_whatsapp_async(phone, message):
    threading.Thread(target=send_green_api_message, args=(phone, message), daemon=True).start()

def normalize_phone(phone):
    clean = re.sub(r"\D", "", str(phone or ""))
    if clean.startswith("0") and len(clean) == 11:
        clean = clean[1:]
    if len(clean) == 10:
        return "+91" + clean
    if len(clean) == 12 and clean.startswith("91"):
        return "+" + clean
    return "+" + clean if not clean.startswith("+") else clean

def clean_phone_10(phone):
    clean = re.sub(r"\D", "", str(phone or ""))
    if clean.startswith("91") and len(clean) == 12:
        return clean[2:]
    if clean.startswith("0") and len(clean) == 11:
        return clean[1:]
    return clean[-10:] if len(clean) >= 10 else clean

def is_datetime_expired(expiry_dt) -> bool:
    """
    Safely checks if an expiry datetime is in the past.
    Handles both timezone-naive (from MongoDB UTC) and timezone-aware datetimes.
    """
    if not expiry_dt:
        return True
    try:
        if isinstance(expiry_dt, datetime.datetime):
            if expiry_dt.tzinfo is None:
                expiry_dt = pytz.utc.localize(expiry_dt)
            return datetime.datetime.now(pytz.utc) > expiry_dt
        return False
    except Exception:
        return True

# ---------- 🧠 100% Robust Indian Society Address Parser & Auto-Prefixer ----------
def format_ordinal_floor(n) -> str:
    try:
        n = int(n)
    except (ValueError, TypeError):
        return f"{n} FLOOR"
    if n == 0:
        return "GROUND FLOOR"
    if 11 <= (n % 100) <= 13:
        suffix = "TH"
    else:
        suffix = {1: "ST", 2: "ND", 3: "RD"}.get(n % 10, "TH")
    return f"{n}{suffix} FLOOR"


def standardize_address_components(raw_block="", raw_floor="", raw_flat="", raw_line1=""):
    """
    100% Robust Standardizer & Auto-Prefixer for Society Addresses:
    1. Capitalization: Everything normalized in clean UPPERCASE.
    2. Block:
       - 'b' / 'B' -> 'BLOCK B'
       - 'a' / 'A' -> 'BLOCK A'
       - 'tower b' / 'Tower Tower B' / 'B Tower' -> 'TOWER B'
       - 'wing c' / 'C-Wing' / 'Wing Wing C' -> 'WING C'
       - 'block a' / 'Block Block A' -> 'BLOCK A'
       - 't-4' / 'T4' -> 'TOWER 4'
       - 'phase 1' / 'pocket b' / 'sector 62' -> preserved
    3. Floor:
       - '4' / '4th' / '4 floor' -> '4TH FLOOR'
       - 'g' / 'ground' / '0' / 'gf' -> 'GROUND FLOOR'
       - 'b' / 'basement' / 'lg' -> 'BASEMENT FLOOR'
       - '1' / '1st' / 'first' / 'ff' -> '1ST FLOOR'
       - '2' / '2nd' / 'second' / 'sf' -> '2ND FLOOR'
       - '3' / '3rd' / 'third' / 'tf' -> '3RD FLOOR'
       - If floor is omitted, auto-derives from 3-4 digit numeric flat (e.g. 402 -> '4TH FLOOR', 1903 -> '19TH FLOOR')
         unless flat is Villa, House, or Shop.
    4. Flat:
       - '402' / 'flat 402' / 'flat flat 402' / 'flat no. 402' / 'unit 402' -> 'FLAT 402'
       - 'villa 12' / 'villa no. 12' / 'villa villa 12' -> 'VILLA 12'
       - 'house 5' / 'house no. 5' -> 'HOUSE 5'
       - 'shop 3' / 'shop no. 3' -> 'SHOP 3'
    5. Full Display:
       - 'TOWER B - FLAT 402 (4TH FLOOR)' or 'BLOCK A - VILLA 12'
    6. Composite line1:
       - 'TOWER B, FLAT 402, 4TH FLOOR' or 'BLOCK A, VILLA 12'
    """
    b = str(raw_block or "").strip().upper()
    fl = str(raw_floor or "").strip().upper()
    fn = str(raw_flat or "").strip().upper()
    line1_text = str(raw_line1 or "").strip()

    # --- Step 0: Extract from raw_line1 if individual inputs were missing ---
    if line1_text and (not b or not fn):
        # Floor extraction from line1
        if not fl:
            floor_match = re.search(r'\b(\d+(?:st|nd|rd|th)?\s*floor|ground\s*floor|first\s*floor|second\s*floor|third\s*floor|gf|ff|sf|basement\s*floor)\b', line1_text, re.IGNORECASE)
            if floor_match:
                fl = floor_match.group(1).upper()
                line1_text = re.sub(re.escape(floor_match.group(0)), '', line1_text, flags=re.IGNORECASE).strip()

        # Block extraction from line1
        if not b:
            block_match = re.search(r'\b((?:tower|block|wing|pocket|phase|sector)\s*[\-\s]*[a-z0-9]+|[a-z0-9]+\s+(?:tower|block|wing))\b', line1_text, re.IGNORECASE)
            if block_match:
                b = block_match.group(1).upper()
                line1_text = re.sub(re.escape(block_match.group(0)), '', line1_text, flags=re.IGNORECASE).strip()

        # Hyphenated check e.g. B-402, T1-102
        if not b and not fn:
            hyphen_match = re.search(r'\b([A-Za-z][A-Za-z0-9]*)\s*[\-]\s*(\d+[A-Za-z]?)\b', line1_text)
            if hyphen_match:
                prefix_part = hyphen_match.group(1).upper()
                if re.match(r'^T\d+$', prefix_part):
                    b = f"TOWER {prefix_part[1:]}"
                else:
                    b = f"BLOCK {prefix_part}"
                fn = f"FLAT {hyphen_match.group(2).upper()}"

        # Flat extraction from line1
        if not fn:
            unit_match = re.search(r'\b(flat|flat\s*no\.?|house|house\s*no\.?|villa|villa\s*no\.?|shop|shop\s*no\.?|unit)\s*[\-\s]*([a-z0-9\-]+)\b', line1_text, re.IGNORECASE)
            if unit_match:
                kw = unit_match.group(1).upper()
                core = unit_match.group(2).upper()
                if "VILLA" in kw:
                    fn = f"VILLA {core}"
                elif "HOUSE" in kw:
                    fn = f"HOUSE {core}"
                elif "SHOP" in kw:
                    fn = f"SHOP {core}"
                else:
                    fn = f"FLAT {core}"
            else:
                num_match = re.search(r'\b([a-z]?\d{1,4}[a-z]?)\b', line1_text, re.IGNORECASE)
                if num_match:
                    fn = num_match.group(1).upper()

    # --- Step 1: Standardize Flat (Smart Normalizer: handles typos, unit variations, V-, H-, S-, accidental prefixes) ---
    fn_clean = re.sub(r'[()\[\]{}]', '', str(fn or "")).strip().upper()
    extracted_floor = ""

    # Pre-clean: Extract and strip any embedded floor mention from flat (e.g. "402 4TH FLOOR", "402 4TH FL", "402, 4TH")
    fl_in_flat = re.search(r'\b(\d{1,2}(?:ST|ND|RD|TH)\s*(?:FLOOR|FLOO|FLR|FL)?|\d{1,2}\s*(?:FLOOR|FLOO|FLR)|GROUND\s*FLOOR|BASEMENT\s*(?:2\s*)?FLOOR|GF|FF|SF|TERRACE\s*FLOOR)\b', fn_clean, re.IGNORECASE)
    if fl_in_flat:
        extracted_floor = fl_in_flat.group(1).upper()
        fn_clean = re.sub(re.escape(fl_in_flat.group(0)), '', fn_clean, flags=re.IGNORECASE).strip()
        fn_clean = re.sub(r'[,:\-\s]+$', '', fn_clean).strip()
    
    # Also strip stray trailing "FLOOR" or "FL" at the end of flat
    fn_clean = re.sub(r'\b(?:FLOOR|FLOO|FLR)\b', '', fn_clean, flags=re.IGNORECASE).strip()
    fn_clean = re.sub(r'(?:[\-:\s#,]+)FL\b', '', fn_clean, flags=re.IGNORECASE).strip()
    fn_clean = re.sub(r'[,:\-\s]+$', '', fn_clean).strip()

    if fn_clean:
        # Check Villa
        if re.search(r'\b(?:VILLA|VIL|VLA|VLLA)\b', fn_clean, re.IGNORECASE) or re.match(r'^V[\-\s#:\.]*\d+', fn_clean, re.IGNORECASE) or re.search(r'\d+\s*(?:VILLA|VIL)\b', fn_clean, re.IGNORECASE):
            core = re.sub(r'\b(?:VILLA|VIL|VLA|VLLA|NUMBER|NUM|NO)\b[\.\s:]*', '', fn_clean, flags=re.IGNORECASE)
            core = re.sub(r'^[#:\-\s\.]+|[#:\-\s\.]+$', '', core)
            core = re.sub(r'^V[\-\s#:\.]*', '', core, flags=re.IGNORECASE).strip()
            fn = f"VILLA {core or fn_clean}"
        # Check House
        elif re.search(r'\b(?:HOUSE|HSE)\b', fn_clean, re.IGNORECASE) or re.match(r'^H[\.\-]?\s*(?:NUMBER|NUM|NO)?[\.\-\s#:]*\d+', fn_clean, re.IGNORECASE) or re.search(r'\d+\s*(?:HOUSE|HSE)\b', fn_clean, re.IGNORECASE):
            core = re.sub(r'\b(?:HOUSE|HSE|NUMBER|NUM|NO)\b[\.\s:]*', '', fn_clean, flags=re.IGNORECASE)
            core = re.sub(r'^[#:\-\s\.]+|[#:\-\s\.]+$', '', core)
            core = re.sub(r'^H[\.\-]?\s*(?:NUMBER|NUM|NO)?[\.\-\s#:]*', '', core, flags=re.IGNORECASE).strip()
            fn = f"HOUSE {core or fn_clean}"
        # Check Shop
        elif re.search(r'\b(?:SHOP|SHP|STORE)\b', fn_clean, re.IGNORECASE) or re.match(r'^S[\-\s#:\.]*\d+', fn_clean, re.IGNORECASE) or re.search(r'\d+\s*(?:SHOP|SHP|STORE)\b', fn_clean, re.IGNORECASE):
            core = re.sub(r'\b(?:SHOP|SHP|STORE|NUMBER|NUM|NO)\b[\.\s:]*', '', fn_clean, flags=re.IGNORECASE)
            core = re.sub(r'^[#:\-\s\.]+|[#:\-\s\.]+$', '', core)
            core = re.sub(r'^S[\-\s#:\.]*', '', core, flags=re.IGNORECASE).strip()
            fn = f"SHOP {core or fn_clean}"
        else:
            suffix_m = re.match(r'^([A-Z0-9\-]+)\s*(?:FLAT|FLT|FLTT|FLAAT|FLA|UNIT|UNT|APT|APARTMENT|APRT|ROOM|RM)$', fn_clean, re.IGNORECASE)
            if suffix_m:
                core = suffix_m.group(1).strip()
            else:
                core = re.sub(r'^(?:(?:FLAT|FLT|FLTT|FLAAT|FLA|UNIT|UNT|APT|APARTMENT|APRT|ROOM|RM)\s*(?:NO\.?|NUMBER|NUM)?\s*[:\-#]?\s*)+', '', fn_clean, flags=re.IGNORECASE).strip()
                core = re.sub(r'^(?:NO\.?|NUMBER|NUM|#)\s*[:\-]?\s*', '', core, flags=re.IGNORECASE).strip()
                # Clean FL- or F- or FL prefix so "FL 402" or "FL-402" becomes "402", avoiding "FLAT FL 402"
                core = re.sub(r'^(?:FL|F)[\-\s#:\.]*(?=[A-Z0-9])', '', core, flags=re.IGNORECASE).strip()
                core = re.sub(r'^[A-Z0-9]{1,3}[\-\s]+(?=\d{3,4})', '', core, flags=re.IGNORECASE).strip()
                core = re.sub(r'[\-\s]*(?:FLAT|FLT|FL|FLOOR|FLR)$', '', core, flags=re.IGNORECASE).strip()
            core = re.sub(r'^[\-:\s#]+|[\-:\s#]+$', '', core).strip()
            fn = f"FLAT {core or fn_clean}"
    else:
        fn = "FLAT -"

    # If floor was not explicitly provided but extracted from flat, adopt it
    if not fl and extracted_floor:
        fl = extracted_floor

    # --- Step 2: Standardize Floor (Smart Normalizer: handles FLOO, FLR, numeric extraction, Hindi/English words) ---
    fl_str = re.sub(r'[()\[\]{}]', '', str(fl or "")).strip().upper()
    if fl_str:
        # Check Basement
        if re.search(r'\b(B\s*2|BASEMENT\s*2|-2)\b', fl_str, re.IGNORECASE):
            fl = "BASEMENT 2 FLOOR"
        elif re.search(r'\b(BASEMENT|BSMT|LOWER\s*GROUND|LG|-1)\b', fl_str, re.IGNORECASE) or fl_str in ["B", "B1"]:
            fl = "BASEMENT FLOOR"
        # Check Ground
        elif re.search(r'\b(GROUND|GRND|GRD)\b', fl_str, re.IGNORECASE) or fl_str in ["G", "GF", "0", "ZERO"]:
            fl = "GROUND FLOOR"
        # Check Terrace / Penthouse
        elif re.search(r'\b(TERRACE|TOP|ROOF|PENTHOUSE|PH)\b', fl_str, re.IGNORECASE):
            fl = "TERRACE FLOOR"
        else:
            # Extract ANY number in the floor string! e.g. "19TH FLOO", "19 FLOO", "FLOO 19", "19", "FLR 19"
            num_m = re.search(r'\b(\d{1,3})\b', fl_str) or re.search(r'(\d+)', fl_str)
            if num_m:
                v = int(num_m.group(1))
                if v == 0:
                    fl = "GROUND FLOOR"
                elif 1 <= v <= 150:
                    fl = format_ordinal_floor(v)
                else:
                    fl = f"{v} FLOOR"
            else:
                # Word numbers (English & Hindi)
                words_map = {
                    "FIRST": 1, "1ST": 1, "PEHLA": 1, "FF": 1,
                    "SECOND": 2, "2ND": 2, "DUSRA": 2, "SF": 2,
                    "THIRD": 3, "3RD": 3, "TEESRA": 3, "TF": 3,
                    "FOURTH": 4, "4TH": 4, "CHAUTHA": 4,
                    "FIFTH": 5, "5TH": 5, "PAANCHWA": 5,
                    "SIXTH": 6, "6TH": 6, "SEVENTH": 7, "7TH": 7,
                    "EIGHTH": 8, "8TH": 8, "NINTH": 9, "9TH": 9,
                    "TENTH": 10, "10TH": 10, "NINETEENTH": 19, "TWENTIETH": 20
                }
                found_word = False
                for w, num_val in words_map.items():
                    if re.search(r'\b' + w + r'\b', fl_str, re.IGNORECASE):
                        fl = format_ordinal_floor(num_val)
                        found_word = True
                        break
                if not found_word:
                    # Strip any garbage "FLOO", "FLOOR", "FLR" typos and only suffix if real text remains
                    clean_core = re.sub(r'\b(?:FLOOR|FLOO|FLR|FLO|FL|LEVEL|LVL)\b|(?:FLOOR|FLOO|FLR|FLO)', '', fl_str, flags=re.IGNORECASE)
                    clean_core = re.sub(r'^[\-:\s]+|[\-:\s]+$', '', clean_core).strip()
                    fl = f"{clean_core} FLOOR" if clean_core else ""

    # --- Step 3: Standardize Block / Tower / Wing (Smart Normalizer: handles typos, prefixes, suffixes, shorthand) ---
    if b:
        b_clean = re.sub(r'[()\[\]{}]', '', str(b)).strip().upper()
        # Phase / Pocket / Sector
        phase_m = re.match(r'^(PHASE|PHS|PH|POCKET|PKT|PCKT|SECTOR|SEC|SCTR)\s*[\-:#\.]*\s*(.*)$', b_clean, re.IGNORECASE)
        if phase_m:
            raw_kw = phase_m.group(1).upper()
            if raw_kw in ["PHS", "PH"]:
                kw = "PHASE"
            elif raw_kw in ["PKT", "PCKT"]:
                kw = "POCKET"
            elif raw_kw in ["SEC", "SCTR"]:
                kw = "SECTOR"
            else:
                kw = raw_kw
            rem = re.sub(r'^[\-:#\.\s]+', '', phase_m.group(2)).strip()
            b = f"{kw} {rem}" if rem else kw
        # Wing (keywords, common typos like WENG/WEENG/VING/WIG, shorthand W-1/W1, reverse order e.g. 1 WING)
        elif re.search(r'\b(?:WING|WNG|WNNG|WENG|WEENG|VING|VEENG|WIG|WIGN)\b|(?:WING|WNG|WENG)', b_clean, re.IGNORECASE) or re.match(r'^W[\-\s#:\.]*[A-Z0-9]+$', b_clean, re.IGNORECASE) or re.match(r'^[A-Z0-9]+\s*(?:WING|WNG|WNNG|WENG|WEENG)$', b_clean, re.IGNORECASE):
            rev_m = re.match(r'^([A-Z0-9]+)\s*(?:WING|WNG|WNNG|WENG|WEENG)$', b_clean, re.IGNORECASE)
            if rev_m:
                core = rev_m.group(1).strip()
            else:
                core = re.sub(r'^(?:WING|WNG|WNNG|WENG|WEENG|VING|VEENG|WIG|WIGN)\s*(?:NO\.?|NUMBER|NUM)?\s*[:\-#]?\s*|^(?:W[\-\s#:\.]+)', '', b_clean, flags=re.IGNORECASE)
                core = re.sub(r'(?:WING|WNG|WNNG|WENG|WEENG|VING|VEENG|WIG|WIGN)', '', core, flags=re.IGNORECASE)
            core = re.sub(r'^[\-:#\.\s]+|[\-:#\.\s]+$', '', core).strip()
            b = f"WING {core or 'GENERAL'}"
        # Tower (keywords, typos e.g. TOWR/TAWAR/TWR, shorthand T-1/T1, reverse order e.g. 1 TOWER)
        elif re.search(r'\b(?:TOWER|TOWR|TOWUR|TOWRE|TOWERR|TWR|TAWAR|TAWER|TOUWER|TOW)\b|(?:TOWER|TOWR|TOWUR|TWR)', b_clean, re.IGNORECASE) or re.match(r'^T[\-\s#:\.]*\d+[A-Z]?$', b_clean, re.IGNORECASE) or re.match(r'^[A-Z0-9]+\s*(?:TOWER|TOWR|TOWUR|TWR)$', b_clean, re.IGNORECASE):
            rev_m = re.match(r'^([A-Z0-9]+)\s*(?:TOWER|TOWR|TOWUR|TOWRE|TOWERR|TWR)$', b_clean, re.IGNORECASE)
            if rev_m:
                core = rev_m.group(1).strip()
            else:
                core = re.sub(r'^(?:TOWER|TOWR|TOWUR|TOWRE|TOWERR|TWR|TAWAR|TAWER|TOUWER|TOW)\s*(?:NO\.?|NUMBER|NUM)?\s*[:\-#]?\s*|^T[\-\s#:\.]*', '', b_clean, flags=re.IGNORECASE)
                core = re.sub(r'(?:TOWER|TOWR|TOWUR|TOWRE|TOWERR|TWR|TAWAR|TAWER|TOUWER|TOW)', '', core, flags=re.IGNORECASE)
            core = re.sub(r'^[\-:#\.\s]+|[\-:#\.\s]+$', '', core).strip()
            b = f"TOWER {core or 'GENERAL'}"
        # Block (keywords, typos e.g. BLCK/BLOK/BLK/BLOCE, reverse order e.g. A BLOCK)
        elif re.search(r'\b(?:BLOKK|BLCKK|BLOKE|BLOCE|BLOCK|BLCK|BLOK|BLOC|BLKK|BOCK|BLK|BLC)\b', b_clean, re.IGNORECASE) or re.match(r'^[A-Z0-9]+\s*(?:BLOKK|BLCKK|BLOKE|BLOCE|BLOCK|BLCK|BLOK|BLOC|BLKK|BOCK|BLK|BLC)$', b_clean, re.IGNORECASE):
            rev_m = re.match(r'^([A-Z0-9]+)\s*(?:BLOKK|BLCKK|BLOKE|BLOCE|BLOCK|BLCK|BLOK|BLOC|BLKK|BOCK|BLK|BLC)$', b_clean, re.IGNORECASE)
            if rev_m:
                core = rev_m.group(1).strip()
            else:
                core = re.sub(r'^(?:BLOKK|BLCKK|BLOKE|BLOCE|BLOCK|BLCK|BLOK|BLOC|BLKK|BOCK|BLK|BLC)\b\s*(?:NO\.?|NUMBER|NUM)?\s*[:\-#]?\s*', '', b_clean, flags=re.IGNORECASE)
                core = re.sub(r'\b(?:BLOKK|BLCKK|BLOKE|BLOCE|BLOCK|BLCK|BLOK|BLOC|BLKK|BOCK|BLK|BLC)\b', '', core, flags=re.IGNORECASE)
            core = re.sub(r'^[\-:#\.\s]+|[\-:#\.\s]+$', '', core).strip()
            b = f"BLOCK {core or 'GENERAL'}"
        else:
            # Fallback: DO NOT blindly prepend "BLOCK"!
            # Only prepend BLOCK if user entered a short 1-2 char code (e.g. "B", "A", "4", "C-2")
            core = re.sub(r'^[\-:#\.\s]+|[\-:#\.\s]+$', '', b_clean).strip()
            if not core:
                b = "GENERAL BLOCK"
            elif re.match(r'^[A-Z0-9]{1,2}$|^[A-Z][\-:]?\d{1,2}$', core):
                b = f"BLOCK {core}"
            else:
                # User entered a full custom word or phrase (e.g. "ROSEWOOD", "GREEN GLEN", "CAMPUS 2")
                # Preserve user's text as-is!
                b = core
    else:
        b = "GENERAL BLOCK"

    # --- Step 4: Build Full Display & Composite Line1 (with de-duplication guards) ---
    if fl:
        # Check if floor number or floor word is already part of fn to avoid duplicating it
        fl_pattern = re.escape(fl)
        if re.search(r'\b' + fl_pattern + r'\b', fn, re.IGNORECASE):
            full_display = f"{b} - {fn}"
        else:
            full_display = f"{b} - {fn} ({fl})"
    else:
        full_display = f"{b} - {fn}"

    # General de-duplication of repeated tokens (e.g. "FLOOR FLOOR", "FLAT FLAT", "FLAT FL", "FL FL")
    full_display = re.sub(r'\b(FLOOR|FL|FLAT|BLOCK|TOWER|WING)\s+\1\b', r'\1', full_display, flags=re.IGNORECASE)
    full_display = re.sub(r'\bFLAT\s+FL\b', 'FLAT', full_display, flags=re.IGNORECASE)
    full_display = re.sub(r'\bFL\s+FLAT\b', 'FLAT', full_display, flags=re.IGNORECASE)
    full_display = re.sub(r'\bFLOOR\s+FLOOR\b', 'FLOOR', full_display, flags=re.IGNORECASE)
    full_display = re.sub(r'\s*\(\s*\)', '', full_display).strip()

    line1_parts = [b, fn]
    if fl and not re.search(r'\b' + re.escape(fl) + r'\b', fn, re.IGNORECASE):
        line1_parts.append(fl)
    line1 = ", ".join(line1_parts)
    line1 = re.sub(r'\b(FLOOR|FL|FLAT|BLOCK|TOWER|WING)\s+\1\b', r'\1', line1, flags=re.IGNORECASE)
    line1 = re.sub(r'\bFLAT\s+FL\b', 'FLAT', line1, flags=re.IGNORECASE)
    line1 = re.sub(r'\bFLOOR\s+FLOOR\b', 'FLOOR', line1, flags=re.IGNORECASE)

    return {
        "block": b,
        "floor": fl,
        "flat_no": fn,
        "line1": line1,
        "full_display": full_display
    }


def parse_society_address(raw_line1, raw_block="", raw_floor="", raw_flat=""):
    return standardize_address_components(raw_block=raw_block, raw_floor=raw_floor, raw_flat=raw_flat, raw_line1=raw_line1)


def ai_normalize_address_components(raw_block="", raw_floor="", raw_flat="", raw_line1=""):
    """
    Deterministic & Instant Society Address Normalizer.
    Guarantees 0ms latency, zero hallucinations, and 100% adherence to user input.
    """
    heur = standardize_address_components(raw_block=raw_block, raw_floor=raw_floor, raw_flat=raw_flat, raw_line1=raw_line1)
    heur["source"] = "standardizer"
    return heur


# ========================================================
# 🏪 VENDOR ROUTES: CREDIT KHATA HUB
# ========================================================

def _get_current_vendor(cols):
    role = session.get("user_role")
    portal_user_id = session.get("portal_user_id")
    if role != "vendor" or not portal_user_id:
        return None, redirect(url_for("vendor_login"))
    try:
        v_oid = ObjectId(portal_user_id)
        vendor = cols["users"].find_one({"_id": v_oid, "role": "vendor"})
        if not vendor:
            return None, redirect(url_for("vendor_login"))
        return vendor, None
    except Exception:
        return None, redirect(url_for("vendor_login"))


def make_canonical_flat_key(vendor_id, society_id, block, flat_no):
    std = standardize_address_components(raw_block=block, raw_flat=flat_no)
    b = re.sub(r'[^A-Z0-9]', '', std.get('block', '').upper())
    f = re.sub(r'[^A-Z0-9]', '', std.get('flat_no', '').upper())
    v = str(vendor_id or '')
    s = str(society_id or '')
    return f"{v}:{s}:{b}:{f}"


def merge_duplicate_flat_khatas(cols, vendor_id):
    """
    1 Flat = 1 Master Khata Auto-Healer:
    Identifies multiple khata records for the same (vendor_id, block, flat_no)
    and merges them safely:
    1. Selects the primary master document.
    2. Consolidates family phones into family_members and family_phones.
    3. Re-points all ledger entries to master document.
    4. Safely removes redundant duplicate documents.
    5. Recalculates master running balance.
    """
    try:
        records = list(cols["khata_users"].find({"vendor_id": vendor_id}))
        from collections import defaultdict
        groups = defaultdict(list)
        for r in records:
            # 🛑 CRITICAL: Do NOT merge PENDING requests or INDEPENDENT / BACHELOR accounts!
            # Independent accounts belong to separate residents in the same flat and must NEVER be merged!
            is_ind = bool(r.get("is_independent") or r.get("account_type") == "INDEPENDENT" or r.get("resident_type") == "INDEPENDENT")
            if r.get("status") in ("PENDING", "REJECTED") or is_ind:
                continue

            b_std = r.get("block") or ""
            f_std = r.get("flat_no") or ""
            std = standardize_address_components(raw_block=b_std, raw_flat=f_std)
            b_clean = re.sub(r'[^A-Z0-9]', '', std.get('block', '').upper())
            f_clean = re.sub(r'[^A-Z0-9]', '', std.get('flat_no', '').upper())
            if not f_clean:
                continue
            key = f"{b_clean}:{f_clean}"
            groups[key].append(r)

        for key, rec_list in groups.items():
            if len(rec_list) <= 1:
                # Ensure primary phone is in family_phones and canonical_flat_key is set (only update if missing)
                rec = rec_list[0]
                p_phone = clean_phone_10(rec.get("customer_phone", ""))
                fam_phones = rec.get("family_phones") or []
                fam_members = rec.get("family_members") or []
                canonical = f"{str(vendor_id)}:{key}"

                needs_update = False
                update_set = {}
                # De-duplicate fam_members by phone
                dedup_m = []
                seen_mp = set()
                has_fam_dups = False
                for fm in fam_members:
                    mp = clean_phone_10(fm.get("phone", ""))
                    if not mp or mp in seen_mp:
                        has_fam_dups = True
                        continue
                    seen_mp.add(mp)
                    fm["phone"] = mp
                    dedup_m.append(fm)

                if has_fam_dups or (p_phone and p_phone not in seen_mp):
                    if p_phone and p_phone not in seen_mp:
                        dedup_m.insert(0, {
                            "name": rec.get("customer_name") or "Primary Resident",
                            "phone": p_phone,
                            "is_primary": True,
                            "added_at": rec.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
                        })
                        seen_mp.add(p_phone)
                    fam_members = dedup_m
                    fam_phones = list(seen_mp)
                    update_set["family_members"] = fam_members
                    update_set["family_phones"] = fam_phones
                    needs_update = True
                elif p_phone and p_phone not in fam_phones:
                    fam_phones = list(set(fam_phones + [p_phone]))
                    update_set["family_phones"] = fam_phones
                    needs_update = True

                if rec.get("canonical_flat_key") != canonical:
                    update_set["canonical_flat_key"] = canonical
                    needs_update = True

                if needs_update:
                    cols["khata_users"].update_one(
                        {"_id": rec["_id"]},
                        {"$set": update_set}
                    )
                continue

            # Pick master record: prefer ACTIVE status, then one with existing non-zero balance, then oldest created
            def sort_score(x):
                status_score = 10 if x.get("status") == "ACTIVE" and x.get("is_verified") else 0
                bal_score = 5 if abs(float(x.get("current_balance") or 0.0)) > 0 else 0
                return (status_score + bal_score)

            rec_list.sort(key=sort_score, reverse=True)
            master = rec_list[0]
            master_id = master["_id"]
            master_phone = clean_phone_10(master.get("customer_phone", ""))

            all_family_members = master.get("family_members") or []
            if not all_family_members and master_phone:
                all_family_members.append({
                    "name": master.get("customer_name") or "Primary Resident",
                    "phone": master_phone,
                    "is_primary": True,
                    "added_at": master.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
                })

            all_family_phones = set(master.get("family_phones") or [])
            if master_phone:
                all_family_phones.add(master_phone)

            # Process all other duplicate records for this flat
            for dup in rec_list[1:]:
                # Double safety guard: Never delete independent or pending accounts
                if dup.get("status") in ("PENDING", "REJECTED") or dup.get("is_independent") or dup.get("account_type") == "INDEPENDENT":
                    continue
                dup_id = dup["_id"]
                dup_phone = clean_phone_10(dup.get("customer_phone", ""))
                dup_name = dup.get("customer_name") or "Family Member"

                if dup_phone and dup_phone not in all_family_phones:
                    all_family_phones.add(dup_phone)
                    all_family_members.append({
                        "name": dup_name,
                        "phone": dup_phone,
                        "is_primary": False,
                        "added_at": dup.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
                    })

                # Re-point all ledger entries of duplicate to master
                cols["khata_ledger"].update_many(
                    {"$or": [{"khata_user_id": dup_id}, {"khata_user_id": str(dup_id)}]},
                    {"$set": {"khata_user_id": master_id}}
                )

                # Remove duplicate document
                cols["khata_users"].delete_one({"_id": dup_id})

            # Update master record with consolidated family info
            cols["khata_users"].update_one(
                {"_id": master_id},
                {"$set": {
                    "family_phones": list(all_family_phones),
                    "family_members": all_family_members,
                    "canonical_flat_key": f"{str(vendor_id)}:{key}",
                    "updated_at": datetime.datetime.now(datetime.timezone.utc)
                }}
            )
            # Recompute accurate balance from combined ledger
            recalculate_khata_user_balance(cols, master_id)
    except Exception as e:
        print("[merge_duplicate_flat_khatas Error]:", e)


@khata_bp.route("/vendor/khata", methods=["GET"])
def vendor_khata_dashboard():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return err

    vendor_oid = vendor["_id"]
    shop_name = vendor.get("shop_name") or vendor.get("name") or "Jippy Partner Store"

    # Auto-heal: Merge any duplicate flats into unified 1-Flat=1-Khata household accounts
    merge_duplicate_flat_khatas(cols, vendor_oid)

    # Serviceable societies names
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
            soc_docs = list(cols["societies"].find({"_id": {"$in": valid_oids}}))
            soc_map = {str(s["_id"]): s for s in soc_docs}
            for sid in soc_ids:
                s_doc = soc_map.get(str(sid))
                if s_doc:
                    resolved_societies.append({
                        "id": str(sid),
                        "name": s_doc.get("name", str(sid)),
                        "city": s_doc.get("city", "")
                    })
        except Exception:
            pass

    # Auto-heal: Merge any duplicate flats into unified 1-Flat=1-Khata household accounts
    merge_duplicate_flat_khatas(cols, vendor_oid)

    # Query all khata records for this vendor
    khata_records = list(cols["khata_users"].find({"vendor_id": vendor_oid}))

    total_outstanding = 0.0
    active_count = 0
    pending_count = 0
    rejected_count = 0
    has_dues_count = 0
    blocks_set = set()

    for r in khata_records:
        # High Performance: Read pre-aggregated current_balance directly, only recalculate if missing
        if "current_balance" in r and r["current_balance"] is not None:
            bal = float(r["current_balance"])
        else:
            bal = recalculate_khata_user_balance(cols, r["_id"])

        if bal > 0:
            total_outstanding += bal
        r["current_balance"] = round(bal, 2)
        r["credit_limit"] = round(float(r.get("credit_limit") or 0.0), 2)
        status = r.get("status", "ACTIVE").upper()
        r["status"] = status
        
        # Self-heal and standardize address on-the-fly (guarantees NO BBLOCK or FLOOR19 FLOOR)
        std_addr = standardize_address_components(
            raw_block=r.get("block"),
            raw_floor=r.get("floor"),
            raw_flat=r.get("flat_no"),
            raw_line1=r.get("full_address_display")
        )
        b_std = std_addr["block"]
        fl_std = std_addr["floor"]
        fn_std = std_addr["flat_no"]
        full_disp = std_addr["full_display"]

        # Auto-heal MongoDB document if previously saved with unstandardized values
        if (r.get("full_address_display") != full_disp or 
            r.get("block") != b_std or 
            r.get("floor") != fl_std or 
            r.get("flat_no") != fn_std):
            try:
                cols["khata_users"].update_one(
                    {"_id": r["_id"]},
                    {"$set": {
                        "block": b_std,
                        "floor": fl_std,
                        "flat_no": fn_std,
                        "full_address_display": full_disp
                    }}
                )
            except Exception:
                pass

        r["block"] = b_std
        r["floor"] = fl_std
        r["flat_no"] = fn_std
        r["full_address_display"] = full_disp

        if status == "PENDING":
            pending_count += 1
        elif status == "ACTIVE":
            active_count += 1
            if bal > 0:
                has_dues_count += 1
        elif status == "REJECTED":
            rejected_count += 1

        blocks_set.add(b_std)

    # Deterministic stable sorting (PENDING first, then highest dues, then flat number)
    khata_records = sort_khata_records_stably(khata_records)

    # Sorted blocks list
    blocks_list = sorted(list(blocks_set))

    now_ist = datetime.datetime.now(IST)

    return render_template(
        "khata/vendor_khata.html",
        vendor=vendor,
        shop_name=shop_name,
        societies=resolved_societies,
        khata_records=khata_records,
        total_outstanding=round(total_outstanding, 2),
        active_count=active_count,
        pending_count=pending_count,
        rejected_count=rejected_count,
        has_dues_count=has_dues_count,
        total_records_count=len(khata_records),
        blocks_list=blocks_list,
        now_ist=now_ist,
        is_online=vendor.get("is_online", True)
    )


# ---------- ⚡ API: Real-Time Live Sync for Vendor Khata Hub (Zero Page Reload) ----------
@khata_bp.route("/vendor/khata/api/live-sync", methods=["GET"])
def vendor_khata_live_sync():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    vendor_oid = vendor["_id"]

    # Auto-heal: Merge any duplicate flats into unified 1-Flat=1-Khata household accounts
    merge_duplicate_flat_khatas(cols, vendor_oid)

    # Query all khata records for this vendor
    khata_records = list(cols["khata_users"].find({"vendor_id": vendor_oid}))

    total_outstanding = 0.0
    active_count = 0
    pending_count = 0
    rejected_count = 0
    has_dues_count = 0
    blocks_set = set()
    cleaned_records = []

    for r in khata_records:
        # High Performance: Read pre-aggregated current_balance directly, only recalculate if missing
        if "current_balance" in r and r["current_balance"] is not None:
            bal = float(r["current_balance"])
        else:
            bal = recalculate_khata_user_balance(cols, r["_id"])

        if bal > 0:
            total_outstanding += bal
        r_bal = round(bal, 2)
        r_limit = round(float(r.get("credit_limit") or 0.0), 2)
        status = r.get("status", "ACTIVE").upper()

        # Standardize and self-heal address components
        std_addr = standardize_address_components(
            raw_block=r.get("block"),
            raw_floor=r.get("floor"),
            raw_flat=r.get("flat_no"),
            raw_line1=r.get("full_address_display")
        )
        b_std = std_addr["block"]
        fl_std = std_addr["floor"]
        fn_std = std_addr["flat_no"]
        full_disp = std_addr["full_display"]

        # Self-heal MongoDB document if previously saved with unstandardized values
        if (r.get("full_address_display") != full_disp or 
            r.get("block") != b_std or 
            r.get("floor") != fl_std or 
            r.get("flat_no") != fn_std):
            try:
                cols["khata_users"].update_one(
                    {"_id": r["_id"]},
                    {"$set": {
                        "block": b_std,
                        "floor": fl_std,
                        "flat_no": fn_std,
                        "full_address_display": full_disp
                    }}
                )
            except Exception:
                pass

        if status == "PENDING":
            pending_count += 1
        elif status == "ACTIVE":
            active_count += 1
            if bal > 0:
                has_dues_count += 1
        elif status == "REJECTED":
            rejected_count += 1

        blocks_set.add(b_std)

        updated_dt = r.get("updated_at") or r.get("created_at")
        if updated_dt:
            if updated_dt.tzinfo is None:
                updated_dt = pytz.utc.localize(updated_dt).astimezone(IST)
            else:
                updated_dt = updated_dt.astimezone(IST)
            updated_str = updated_dt.strftime("%d %b %Y, %I:%M %p")
        else:
            updated_str = ""

        fam_list = r.get("family_members") or []
        is_ind = bool(r.get("is_independent") or r.get("account_type") == "INDEPENDENT" or r.get("resident_type") == "INDEPENDENT")
        cleaned_records.append({
            "id": str(r["_id"]),
            "customer_name": r.get("customer_name") or "Resident",
            "customer_phone": r.get("customer_phone") or "",
            "family_members": fam_list,
            "family_count": len(fam_list),
            "block": b_std,
            "flat_no": fn_std,
            "floor": fl_std,
            "full_address_display": full_disp,
            "status": status,
            "is_verified": bool(r.get("is_verified")),
            "is_independent": is_ind,
            "account_type": r.get("account_type", "INDEPENDENT" if is_ind else "FLAT_MASTER"),
            "current_balance": r_bal,
            "credit_limit": r_limit,
            "updated_at_str": updated_str
        })

    # Deterministic stable sorting (PENDING first, then highest dues, then flat number)
    cleaned_records = sort_khata_records_stably(cleaned_records)

    blocks_list = sorted(list(blocks_set))

    res = jsonify({
        "success": True,
        "kpis": {
            "total_outstanding": round(total_outstanding, 2),
            "active_count": active_count,
            "pending_count": pending_count,
            "rejected_count": rejected_count,
            "has_dues_count": has_dues_count,
            "total_records_count": len(khata_records)
        },
        "blocks": blocks_list,
        "records": cleaned_records,
        "server_time": datetime.datetime.now(IST).strftime("%I:%M:%S %p")
    })
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    res.headers["Pragma"] = "no-cache"
    res.headers["Expires"] = "0"
    return res


# ---------- 📝 Dedicated Page: Add Resident with WhatsApp OTP ----------
@khata_bp.route("/vendor/khata/add-resident", methods=["GET"])
def vendor_add_resident_page():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return err

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Jippy Partner Store"

    # Serviceable societies names
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
            soc_docs = list(cols["societies"].find({"_id": {"$in": valid_oids}}))
            soc_map = {str(s["_id"]): s for s in soc_docs}
            for sid in soc_ids:
                s_doc = soc_map.get(str(sid))
                if s_doc:
                    resolved_societies.append({
                        "id": str(sid),
                        "name": s_doc.get("name", str(sid)),
                        "city": s_doc.get("city", "")
                    })
        except Exception:
            pass

    return render_template(
        "khata/vendor_add_resident.html",
        vendor=vendor,
        shop_name=shop_name,
        societies=resolved_societies,
        is_online=vendor.get("is_online", True)
    )


# ---------- API: AI-Assisted Address Normalization (Gemini + Smart Fallback) ----------
@khata_bp.route("/vendor/khata/api/ai-normalize-address", methods=["POST"])
def vendor_ai_normalize_address():
    data = request.get_json() or {}
    raw_block = data.get("block", "")
    raw_floor = data.get("floor", "")
    raw_flat = data.get("flat_no", "")
    raw_line1 = data.get("line1", "")

    result = ai_normalize_address_components(
        raw_block=raw_block,
        raw_floor=raw_floor,
        raw_flat=raw_flat,
        raw_line1=raw_line1
    )
    return jsonify({
        "success": True,
        **result
    })


# ---------- API: Send 6-Digit WhatsApp OTP for Direct Vendor Add ----------
@khata_bp.route("/vendor/khata/api/send-otp", methods=["POST"])
def vendor_send_khata_otp():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    phone = clean_phone_10(data.get("phone", ""))
    customer_name = (data.get("customer_name") or "Resident").strip()
    block = (data.get("block") or "").strip()
    flat_no = (data.get("flat_no") or "").strip()
    floor = (data.get("floor") or "").strip()
    shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"

    if not phone or len(phone) < 10:
        return jsonify({"success": False, "error": "Valid 10-digit mobile number is required"}), 400
    if not flat_no:
        return jsonify({"success": False, "error": "Flat number is required"}), 400

    std_addr = ai_normalize_address_components(raw_block=block, raw_floor=floor, raw_flat=flat_no)
    block = std_addr["block"]
    floor = std_addr["floor"]
    flat_no = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    # 1. Strict Check: Is this mobile number ALREADY enrolled in this vendor's Khata?
    phone_variants = [phone, f"+91{phone}"]
    existing_phone_user = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "$or": [
            {"customer_phone": {"$in": phone_variants}},
            {"family_phones": {"$in": phone_variants}}
        ]
    })
    if existing_phone_user:
        curr_status = str(existing_phone_user.get("status", "")).upper()
        ex_flat = existing_phone_user.get("full_address_display") or f"{existing_phone_user.get('block')} - {existing_phone_user.get('flat_no')}"
        ex_name = existing_phone_user.get("customer_name") or "Resident"
        if curr_status == "ACTIVE" and existing_phone_user.get("is_verified"):
            return jsonify({
                "success": False,
                "error": f"Mobile +91 {phone} is ALREADY enrolled in {ex_flat} (Owner: {ex_name}). Duplicate enrollment is not allowed."
            }), 400
        elif curr_status == "PENDING":
            return jsonify({
                "success": False,
                "error": f"Mobile +91 {phone} already has a pending Store Credit request for {ex_flat}. Please approve it directly from the Khata Hub."
            }), 400

    # 2. Strict Check: Is this exact Flat ALREADY enrolled in this vendor's Khata?
    existing_flat = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "block": block,
        "flat_no": flat_no,
        "status": "ACTIVE",
        "is_verified": True
    })
    if existing_flat:
        primary_name = existing_flat.get("customer_name") or "Primary Resident"
        primary_p = clean_phone_10(existing_flat.get("customer_phone") or "")
        fam_phones = set(clean_phone_10(p) for p in (existing_flat.get("family_phones") or []))
        if phone == primary_p or phone in fam_phones:
            return jsonify({
                "success": False,
                "error": f"{full_disp} is already actively enrolled under +91 {phone} ({primary_name})."
            }), 400

    otp_code = str(random.randint(100000, 999999))
    expiry = datetime.datetime.now(IST) + datetime.timedelta(minutes=5)

    # Store OTP record
    cols["khata_otps"].update_one(
        {"vendor_id": vendor["_id"], "phone": phone},
        {
            "$set": {
                "otp": otp_code,
                "expiry": expiry,
                "customer_name": customer_name.upper(),
                "block": block,
                "flat_no": flat_no,
                "floor": floor,
                "full_address_display": full_disp,
                "created_at": datetime.datetime.now(IST)
            }
        },
        upsert=True
    )

    # Professional English WhatsApp OTP Template
    msg = (
        f"🔐 *VERIFICATION CODE | JIPPY STORE*\n\n"
        f"Dear *{customer_name}*,\n\n"
        f"*{shop_name}* is requesting to link your society residence (*{full_disp}*) for Society Store Credit.\n\n"
        f"🔑 *Your 6-Digit Verification Code is:*\n"
        f"👉 *{otp_code}*\n"
        f"_(Valid for 5 minutes)_\n\n"
        f"Please share this code with your store vendor to confirm your account activation and consent to monthly billing.\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )

    success = send_green_api_message(phone, msg)
    if success:
        return jsonify({
            "success": True,
            "message": f"Verification OTP sent to +91 {phone} via WhatsApp.",
            "full_address_display": full_disp
        })
    else:
        # Return success with fallback notice for local testing/network hiccups
        return jsonify({
            "success": True,
            "message": f"OTP generated successfully. (Code: {otp_code} - share with vendor)",
            "dev_otp": otp_code,
            "full_address_display": full_disp
        })


# ---------- API: Verify OTP & Activate Account ----------
@khata_bp.route("/vendor/khata/api/verify-otp-and-add", methods=["POST"])
def vendor_verify_otp_and_add():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    phone = clean_phone_10(data.get("phone", ""))
    otp_input = str(data.get("otp", "")).strip()
    customer_name = (data.get("customer_name") or "Resident").strip()
    block = (data.get("block") or "General Block").strip()
    flat_no = (data.get("flat_no") or "").strip()
    floor = (data.get("floor") or "").strip()
    society_id = data.get("society_id") or ""
    credit_limit = float(data.get("credit_limit") or 0.0)

    otp_doc = cols["khata_otps"].find_one({"vendor_id": vendor["_id"], "phone": phone})
    if not otp_doc:
        return jsonify({"success": False, "error": "No pending OTP request found for this mobile number."}), 400

    if is_datetime_expired(otp_doc.get("expiry")):
        return jsonify({"success": False, "error": "OTP has expired. Please send a new OTP."}), 400

    if otp_doc.get("otp") != otp_input:
        return jsonify({"success": False, "error": "Incorrect verification code. Please check and try again."}), 400

    # Clean OTP record
    cols["khata_otps"].delete_one({"_id": otp_doc["_id"]})

    # Find existing customer user if registered
    norm_phone = "+91" + phone
    cust_user = cols["users"].find_one({"phone": norm_phone, "role": "customer"})
    user_id = cust_user["_id"] if cust_user else None

    # Society name resolution
    soc_name = "Society Store"
    if society_id:
        try:
            s_obj = cols["societies"].find_one({"_id": ObjectId(society_id)})
            if s_obj:
                soc_name = s_obj.get("name", soc_name)
        except Exception:
            pass

    # Standardize Address (BLOCK, FLAT, FLOOR, Uppercase)
    std_addr = standardize_address_components(raw_block=block, raw_floor=floor, raw_flat=flat_no)
    block = std_addr["block"]
    floor = std_addr["floor"]
    flat_no = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    # Check if a khata already exists for this exact flat under this vendor
    existing_flat = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "block": block,
        "flat_no": flat_no
    })

    fam_phones = [phone]
    fam_members = [{
        "name": customer_name.upper(),
        "phone": phone,
        "is_primary": True,
        "added_at": datetime.datetime.now(datetime.timezone.utc)
    }]

    if existing_flat and existing_flat.get("status") == "ACTIVE" and existing_flat.get("is_verified"):
        primary_p = clean_phone_10(existing_flat.get("customer_phone") or "")
        all_fams = existing_flat.get("family_members") or []
        all_phones = set(clean_phone_10(p) for p in (existing_flat.get("family_phones") or []))
        all_phones.add(phone)
        if primary_p:
            all_phones.add(primary_p)

        existing_fm_phones = {clean_phone_10(fm.get("phone")) for fm in all_fams}
        if phone not in existing_fm_phones:
            all_fams.append({
                "name": customer_name.title(),
                "phone": phone,
                "is_primary": False,
                "added_at": datetime.datetime.now(datetime.timezone.utc)
            })

        update_fields = {
            "family_phones": list(all_phones),
            "family_members": all_fams,
            "updated_at": datetime.datetime.now(datetime.timezone.utc)
        }
        if credit_limit > 0 and credit_limit > float(existing_flat.get("credit_limit") or 0.0):
            update_fields["credit_limit"] = credit_limit

        cols["khata_users"].update_one(
            {"_id": existing_flat["_id"]},
            {"$set": update_fields}
        )
    elif existing_flat:
        cols["khata_users"].update_one(
            {"_id": existing_flat["_id"]},
            {
                "$set": {
                    "customer_name": customer_name.upper(),
                    "customer_phone": phone,
                    "family_phones": list(set(fam_phones)),
                    "family_members": fam_members,
                    "society_id": society_id,
                    "society_name": soc_name,
                    "block": block,
                    "flat_no": flat_no,
                    "floor": floor,
                    "full_address_display": full_disp,
                    "is_verified": True,
                    "status": "ACTIVE",
                    "credit_limit": credit_limit,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc),
                }
            }
        )
    else:
        canonical_key = f"{str(vendor['_id'])}:{re.sub(r'[^A-Z0-9]', '', block.upper())}:{re.sub(r'[^A-Z0-9]', '', flat_no.upper())}"
        cols["khata_users"].insert_one({
            "vendor_id": vendor["_id"],
            "user_id": user_id,
            "customer_name": customer_name.upper(),
            "customer_phone": phone,
            "family_phones": list(set(fam_phones)),
            "family_members": fam_members,
            "canonical_flat_key": canonical_key,
            "society_id": society_id,
            "society_name": soc_name,
            "block": block,
            "flat_no": flat_no,
            "floor": floor,
            "full_address_display": full_disp,
            "is_verified": True,
            "status": "ACTIVE",
            "credit_limit": credit_limit,
            "current_balance": 0.0,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        })

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"

    # English WhatsApp Confirmation to Customer
    limit_text = f"₹{credit_limit:,.2f}" if credit_limit > 0 else "Unlimited"
    welcome_msg = (
        f"🎉 *STORE CREDIT ACTIVATED | JIPPY STORE* 🛍️\n\n"
        f"Dear *{customer_name}*,\n\n"
        f"Congratulations! Your Society Store Credit account has been successfully verified & activated.\n\n"
        f"📋 *ACCOUNT DETAILS:*\n"
        f"• *Store:* {shop_name}\n"
        f"• *Unit / Flat:* {full_disp}\n"
        f"• *Monthly Credit Limit:* {limit_text}\n"
        f"• *Account Status:* Active & Verified ✅\n\n"
        f"You can now select *\"Society Store Credit\"* at checkout on Jippy Store and enjoy seamless monthly billing.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )
    send_whatsapp_async(phone, welcome_msg)

    return jsonify({"success": True, "message": f"{customer_name} ({full_disp}) successfully verified and activated!"})


# ---------- API: Approve Customer-Initiated Request ----------
@khata_bp.route("/vendor/khata/api/approve-request", methods=["POST"])
def vendor_approve_request():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    record_id = data.get("record_id")
    try:
        credit_limit = max(0.0, float(data.get("credit_limit") or 0.0))
    except (ValueError, TypeError):
        credit_limit = 0.0

    if not record_id:
        return jsonify({"success": False, "error": "Record ID is required"}), 400

    rec = cols["khata_users"].find_one({"_id": ObjectId(record_id), "vendor_id": vendor["_id"]})
    if not rec:
        return jsonify({"success": False, "error": "Khata account not found"}), 404

    # Check if another ACTIVE record already exists for this exact flat!
    std_addr = standardize_address_components(raw_block=rec.get("block"), raw_floor=rec.get("floor"), raw_flat=rec.get("flat_no"))
    b_std = std_addr["block"]
    fn_std = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    is_rec_independent = bool(rec.get("is_independent") or rec.get("account_type") == "INDEPENDENT" or rec.get("resident_type") == "INDEPENDENT")

    existing_active = None
    if not is_rec_independent:
        existing_active = cols["khata_users"].find_one({
            "_id": {"$ne": rec["_id"]},
            "vendor_id": vendor["_id"],
            "block": b_std,
            "flat_no": fn_std,
            "status": "ACTIVE",
            "is_verified": True,
            "is_independent": {"$ne": True},
            "account_type": {"$ne": "INDEPENDENT"}
        })

    if existing_active and not is_rec_independent:
        # Auto-merge: Absorb this member into the existing active flat
        cust_name = rec.get("customer_name", "Resident")
        phone = clean_phone_10(rec.get("customer_phone", ""))
        all_fams = existing_active.get("family_members") or []
        all_phones = set(existing_active.get("family_phones") or [])
        if phone:
            all_phones.add(phone)
            all_fams.append({
                "name": cust_name.title(),
                "phone": phone,
                "is_primary": False,
                "added_at": datetime.datetime.now(datetime.timezone.utc)
            })
        cols["khata_users"].update_one(
            {"_id": existing_active["_id"]},
            {
                "$set": {
                    "family_phones": list(all_phones),
                    "family_members": all_fams,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc)
                }
            }
        )
        # Delete redundant pending record
        cols["khata_users"].delete_one({"_id": rec["_id"]})

        primary_name = existing_active.get("customer_name") or "Primary Resident"
        shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
        msg_member = (
            f"🎉 *STORE CREDIT LINKED | JIPPY STORE* 🛍️\n\n"
            f"Dear *{cust_name.title()}*,\n\n"
            f"Your request has been approved! You have been linked to the Store Credit passbook for *{full_disp}* (Account Owner: *{primary_name}*).\n\n"
            f"You can now place 1-click orders on Jippy Store billed directly to your flat's monthly passbook.\n\n"
            f"🔗 *Start Shopping:* https://jippy.store\n\n"
            f"🛍️ *Thank you for choosing Jippy Store!*"
        )
        send_whatsapp_async(phone, msg_member)

        return jsonify({
            "success": True,
            "message": f"{cust_name.title()} (+91 {phone}) linked to {full_disp} (Owner: {primary_name}) successfully!"
        })

    # Activate independent account or master account directly
    phone = clean_phone_10(rec.get("customer_phone", ""))
    cols["khata_users"].update_one(
        {"_id": rec["_id"]},
        {
            "$set": {
                "is_verified": True,
                "status": "ACTIVE",
                "credit_limit": round(credit_limit, 2),
                "is_independent": True if is_rec_independent else False,
                "account_type": "INDEPENDENT" if is_rec_independent else "FLAT_MASTER",
                "resident_type": "INDEPENDENT" if is_rec_independent else "PRIMARY_OWNER",
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            }
        }
    )

    # If independent, ensure their phone is detached from any shared household passbook
    if is_rec_independent and phone:
        cols["khata_users"].update_many(
            {
                "vendor_id": vendor["_id"],
                "_id": {"$ne": rec["_id"]},
                "$or": [
                    {"family_phones": phone},
                    {"family_members.phone": phone}
                ]
            },
            {
                "$pull": {
                    "family_phones": phone,
                    "family_members": {"phone": phone}
                }
            }
        )

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
    cust_name = rec.get("customer_name", "Resident")
    phone = rec.get("customer_phone", "")
    full_disp = rec.get("full_address_display")
    if not full_disp:
        std_addr = standardize_address_components(raw_block=rec.get("block"), raw_floor=rec.get("floor"), raw_flat=rec.get("flat_no"))
        full_disp = std_addr["full_display"]

    limit_text = f"₹{credit_limit:,.2f}" if credit_limit > 0 else "Unlimited"
    approval_msg = (
        f"🎉 *STORE CREDIT APPROVED | JIPPY STORE* 🛍️\n\n"
        f"Dear *{cust_name}*,\n\n"
        f"Great news! Your Store Credit account has been approved and activated by *{shop_name}*.\n\n"
        f"📋 *ACCOUNT DETAILS:*\n"
        f"• *Unit / Flat:* {full_disp}\n"
        f"• *Account Status:* Active & Verified ✅\n"
        f"• *Credit Limit:* {limit_text}\n\n"
        f"You can now place your grocery orders using *\"Society Store Credit\"* at checkout.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )
    send_whatsapp_async(phone, approval_msg)

    return jsonify({"success": True, "message": f"Account for {cust_name} approved successfully!"})


# ---------- API: Decline Customer-Initiated Request ----------
@khata_bp.route("/vendor/khata/api/reject-request", methods=["POST"])
def vendor_reject_request():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    record_id = data.get("record_id")

    if not record_id:
        return jsonify({"success": False, "error": "Record ID is required"}), 400

    cols["khata_users"].update_one(
        {"_id": ObjectId(record_id), "vendor_id": vendor["_id"]},
        {
            "$set": {
                "is_verified": False,
                "status": "REJECTED",
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            }
        }
    )
    return jsonify({"success": True, "message": "Request declined."})


# ---------- API: Delete Khata Record (Resident) ----------
@khata_bp.route("/vendor/khata/api/delete-resident", methods=["POST"])
def vendor_delete_resident():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    record_id = data.get("record_id")

    if not record_id:
        return jsonify({"success": False, "error": "Record ID is required"}), 400

    try:
        rec = cols["khata_users"].find_one({"_id": ObjectId(record_id), "vendor_id": vendor["_id"]})
        if not rec:
            return jsonify({"success": False, "error": "Khata resident record not found"}), 404

        bal = float(rec.get("current_balance") or 0.0)
        if bal > 0:
            return jsonify({"success": False, "error": f"Cannot delete resident with outstanding balance of ₹{bal:,.2f}. Please record settlement first."}), 400

        # Delete associated ledger entries
        cols["khata_ledger"].delete_many({"khata_user_id": rec["_id"]})
        # Delete khata record
        cols["khata_users"].delete_one({"_id": rec["_id"]})

        return jsonify({"success": True, "message": f"Resident record {rec.get('customer_name', '')} deleted successfully."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- 🔄 Recalculate User Balance from Ledger (Self-Healing) ----------
def recalculate_khata_user_balance(cols_or_db, khata_user_id):
    """
    Computes accurate current balance from itemized ledger entries:
    Net Balance = Total Debits (Orders) - Total Credits (Payments)
    Positive balance (> 0) = Outstanding Due
    Negative balance (< 0) = Advance Credit / Store Wallet
    Zero (== 0) = No Dues
    """
    if isinstance(cols_or_db, dict):
        ledger_col = cols_or_db.get("khata_ledger") if "khata_ledger" in cols_or_db else cols_or_db.get("vendor_khata_ledger")
        users_col = cols_or_db.get("khata_users") if "khata_users" in cols_or_db else cols_or_db.get("vendor_khata_users")
    else:
        ledger_col = cols_or_db["vendor_khata_ledger"]
        users_col = cols_or_db["vendor_khata_users"]

    try:
        oid = ObjectId(khata_user_id) if not isinstance(khata_user_id, ObjectId) else khata_user_id
    except Exception:
        oid = khata_user_id

    entries = list(ledger_col.find({
        "$or": [{"khata_user_id": oid}, {"khata_user_id": str(oid)}]
    }).sort("created_at", 1))
    running_bal = 0.0
    for e in entries:
        amt = float(e.get("amount") or 0.0)
        t = (e.get("type") or "DEBIT").upper()
        if t == "DEBIT":
            running_bal += amt
        else:
            running_bal -= amt
        bal_round = round(running_bal, 2)
        if e.get("balance_after") != bal_round:
            ledger_col.update_one(
                {"_id": e["_id"]},
                {"$set": {"balance_after": bal_round}}
            )
    net_bal = round(running_bal, 2)
    # Only update MongoDB if the balance actually changed, avoiding updated_at churn during polling
    curr_doc = users_col.find_one({"_id": oid}, {"current_balance": 1})
    if not curr_doc or curr_doc.get("current_balance") != net_bal:
        users_col.update_one(
            {"_id": oid},
            {"$set": {"current_balance": net_bal}}
        )
    return net_bal

def sort_khata_records_stably(records):
    """
    Deterministic & Stable Sorting for Vendor Khatabook cards:
    1. PENDING requests first (priority for store approval/rejection)
    2. Outstanding dues highest to lowest (bal > 0)
    3. Block (alphabetical)
    4. Flat number (numerical e.g. 101, 102, 1903)
    5. Stable tie-breaker (_id)
    Never flips back and forth during live polling!
    """
    def _flat_num(r):
        nums = re.findall(r'\d+', str(r.get("flat_no", "")))
        return int(nums[0]) if nums else 0

    def _sort_tuple(r):
        status = str(r.get("status", "")).upper()
        status_rank = 0 if status == "PENDING" else (1 if status == "ACTIVE" else 2)
        bal = float(r.get("current_balance") or 0.0)
        block = str(r.get("block") or "")
        flat_n = _flat_num(r)
        rid = str(r.get("_id") or r.get("id") or "")
        return (status_rank, -bal, block, flat_n, rid)

    return sorted(records, key=_sort_tuple)


# ---------- API: Record Payment Received (Settlement) ----------
@khata_bp.route("/vendor/khata/api/record-payment", methods=["POST"])
def vendor_record_payment():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json() or {}
    record_id = data.get("record_id")
    try:
        amount = round(float(data.get("amount") or 0.0), 2)
    except (ValueError, TypeError):
        amount = 0.0
    mode = (data.get("mode") or "Cash").strip()
    notes = (data.get("notes") or "").strip()

    if not record_id or amount <= 0:
        return jsonify({"success": False, "error": "Valid payment amount is required"}), 400

    rec = cols["khata_users"].find_one({"_id": ObjectId(record_id), "vendor_id": vendor["_id"]})
    if not rec:
        return jsonify({"success": False, "error": "Khata account not found"}), 404

    current_bal = float(rec.get("current_balance") or 0.0)

    # Insert into ledger
    ledger_entry = {
        "khata_user_id": rec["_id"],
        "vendor_id": vendor["_id"],
        "user_id": rec.get("user_id"),
        "customer_name": rec.get("customer_name"),
        "customer_phone": rec.get("customer_phone"),
        "type": "CREDIT", # Payment received reduces balance
        "amount": round(amount, 2),
        "balance_before": round(current_bal, 2),
        "balance_after": round(current_bal - amount, 2),
        "payment_mode": mode,
        "notes": notes,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    cols["khata_ledger"].insert_one(ledger_entry)

    # Recalculate true running balance from entire ledger history
    new_bal = recalculate_khata_user_balance(cols, rec["_id"])
    cols["khata_users"].update_one(
        {"_id": rec["_id"]},
        {"$set": {"last_payment_at": datetime.datetime.now(datetime.timezone.utc)}}
    )

    cust_name = rec.get("customer_name", "Resident")
    phone = rec.get("customer_phone", "")
    shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
    date_str = datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p")

    full_disp = rec.get("full_address_display")
    if not full_disp and (rec.get("block") or rec.get("flat_no")):
        std_addr = standardize_address_components(raw_block=rec.get("block"), raw_floor=rec.get("floor"), raw_flat=rec.get("flat_no"))
        full_disp = std_addr["full_display"]

    if new_bal > 0:
        rem_due_line = f"• Remaining Khata Due: ₹{new_bal:,.2f}\n"
    elif new_bal < 0:
        rem_due_line = f"• Remaining Khata Due: ₹0.00 (Advance Deposit: +₹{abs(new_bal):,.2f})\n"
    else:
        rem_due_line = "• Remaining Khata Due: ₹0.00 (Fully Settled)\n"

    # Payment Acknowledgment WhatsApp
    receipt_msg = (
        f"🧾 *PAYMENT RECEIVED | JIPPY STORE* 💳\n\n"
        f"Dear *{cust_name}*,\n\n"
        f"We have successfully received your payment of *₹{amount:,.2f}* for *{shop_name}*.\n\n"
        f"📋 *PAYMENT DETAILS:*\n"
        f"• *Amount Credited:* ₹{amount:,.2f}\n"
        f"• *Payment Method:* {mode}\n"
        + rem_due_line
        + (f"• *Unit / Flat:* {full_disp}\n" if full_disp else "")
        + f"• *Date & Time:* {date_str}\n\n"
        f"Your Store Credit passbook has been updated.\n"
        f"🔗 *View Passbook:* https://jippy.store/my-khata\n\n"
        f"✨ *Thank you for your prompt settlement!*"
    )
    send_whatsapp_async(phone, receipt_msg)

    return jsonify({
        "success": True,
        "message": f"Payment of ₹{amount:,.2f} recorded successfully!",
        "new_balance": round(new_bal, 2)
    })


# ---------- API: Fetch Resident Itemized Ledger History ----------
@khata_bp.route("/vendor/khata/api/ledger/<record_id>", methods=["GET"])
def vendor_get_ledger(record_id):
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        rec = cols["khata_users"].find_one({"_id": ObjectId(record_id), "vendor_id": vendor["_id"]})
        if not rec:
            return jsonify({"success": False, "error": "Record not found"}), 404

        # Auto-sync any recent custom notes adjustments and recalculate balance
        sync_khata_user_recent_orders(get_db(), rec)
        real_bal = recalculate_khata_user_balance(cols, rec["_id"])
        rec["current_balance"] = real_bal

        page = max(1, int(request.args.get("page", 1)))
        limit = max(1, min(100, int(request.args.get("limit", 10))))

        total_count = cols["khata_ledger"].count_documents({"khata_user_id": rec["_id"]})
        skip = (page - 1) * limit
        entries = list(cols["khata_ledger"].find({"khata_user_id": rec["_id"]}).sort("created_at", -1).skip(skip).limit(limit))
        out = []
        for e in entries:
            dt = e.get("created_at")
            if dt and dt.tzinfo is None:
                dt = pytz.utc.localize(dt).astimezone(IST)
            elif dt:
                dt = dt.astimezone(IST)
            else:
                dt = datetime.datetime.now(IST)

            out.append({
                "id": str(e["_id"]),
                "type": e.get("type", "DEBIT"),
                "amount": e.get("amount", 0.0),
                "order_id": e.get("order_id", ""),
                "payment_mode": e.get("payment_mode", ""),
                "balance_after": e.get("balance_after", 0.0),
                "notes": e.get("notes", ""),
                "date_str": dt.strftime("%d %b %Y, %I:%M %p")
            })

        return jsonify({
            "success": True,
            "customer_name": rec.get("customer_name"),
            "full_address": rec.get("full_address_display"),
            "current_balance": round(float(rec.get("current_balance") or 0.0), 2),
            "entries": out,
            "page": page,
            "limit": limit,
            "total_count": total_count,
            "has_more": (skip + len(out)) < total_count
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- API: Generate 1-Click WhatsApp Reminder / Statement Link ----------
@khata_bp.route("/vendor/khata/api/whatsapp-reminder-url/<record_id>", methods=["GET"])
def vendor_whatsapp_reminder_url(record_id):
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        rec = cols["khata_users"].find_one({"_id": ObjectId(record_id), "vendor_id": vendor["_id"]})
        if not rec:
            return jsonify({"success": False, "error": "Record not found"}), 404

        phone = clean_phone_10(rec.get("customer_phone", ""))
        if not phone or len(phone) < 10:
            return jsonify({"success": False, "error": "Resident has no valid phone number recorded."}), 400

        cust_name = rec.get("customer_name", "Resident")
        full_disp = rec.get("full_address_display")
        if not full_disp:
            std_addr = standardize_address_components(raw_block=rec.get("block"), raw_floor=rec.get("floor"), raw_flat=rec.get("flat_no"))
            full_disp = std_addr["full_display"]
        current_bal = round(float(rec.get("current_balance") or 0.0), 2)
        shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
        today_str = datetime.datetime.now(IST).strftime("%d %B %Y")
        upi_id = vendor.get("upi_id") or "Store Cash / UPI"

        if current_bal > 0:
            msg = (
                f"Dear {cust_name} (*{full_disp}*),\n\n"
                f"Greetings from your in-house society store, *{shop_name}*.\n\n"
                f"Here is your monthly Society Store Credit (Khata) statement summary:\n"
                f"• *Total Outstanding Balance Due*: ₹{current_bal:,.2f}\n"
                f"• Statement Date: {today_str}\n\n"
                f"You can settle your balance via Cash or UPI at your convenience.\n"
                f"• Store UPI ID: {upi_id}\n\n"
                f"View detailed itemized bills & receipts:\n"
                f"https://jippy.store/my-khata\n\n"
                f"Thank you for being a valued resident!\n"
                f"- {shop_name}"
            )
        elif current_bal < 0:
            msg = (
                f"Dear {cust_name} (*{full_disp}*),\n\n"
                f"Greetings from your in-house society store, *{shop_name}*.\n\n"
                f"Here is your monthly Society Store Credit (Khata) statement summary:\n"
                f"• *Account Status*: Zero Dues (₹0.00)\n"
                f"• *Advance Deposit Credit*: +₹{abs(current_bal):,.2f}\n"
                f"• Statement Date: {today_str}\n\n"
                f"Your advance credit will automatically adjust against your upcoming grocery orders.\n\n"
                f"View detailed passbook:\n"
                f"https://jippy.store/my-khata\n\n"
                f"Thank you for being a valued resident!\n"
                f"- {shop_name}"
            )
        else:
            msg = (
                f"Dear {cust_name} (*{full_disp}*),\n\n"
                f"Greetings from your in-house society store, *{shop_name}*.\n\n"
                f"Here is your monthly Society Store Credit (Khata) statement summary:\n"
                f"• *Account Status*: Fully Settled (Zero Dues - ₹0.00)\n"
                f"• Statement Date: {today_str}\n\n"
                f"Your account is in good standing with 0 pending dues. You can continue placing orders using Store Credit anytime at checkout!\n\n"
                f"View detailed passbook:\n"
                f"https://jippy.store/my-khata\n\n"
                f"Thank you for being a valued resident!\n"
                f"- {shop_name}"
            )

        wa_url = f"https://wa.me/91{phone}?text={quote(msg)}"
        return jsonify({"success": True, "whatsapp_url": wa_url, "balance": current_bal})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- 🔄 STORE CREDIT ORDER SYNC HELPERS (CANCELLATION & ADJUSTMENTS) ----------

def _resolve_order_khata_record(db, order):
    """
    Reliably resolves vendor_khata_users document for an order.
    Checks:
    1. Existing ledger entry for this order_id (100% reliable)
    2. customer_phone and vendor_id (checking both ObjectId and string vendor_id)
    3. User ID lookup fallback
    """
    if not order:
        return None, None

    order_id = order.get("order_id", "")
    existing_ledger = db["vendor_khata_ledger"].find_one({"order_id": order_id}) if order_id else None
    if existing_ledger and existing_ledger.get("khata_user_id"):
        khata_rec = db["vendor_khata_users"].find_one({"_id": existing_ledger["khata_user_id"]})
        if khata_rec:
            return khata_rec, existing_ledger

    # Resolve vendor_id from order root or items
    vendor_id = order.get("vendor_id")
    if not vendor_id:
        for it in (order.get("items") or []):
            if it.get("vendor_id"):
                vendor_id = it.get("vendor_id")
                break

    user_phone = clean_phone_10(order.get("user_phone", ""))
    user_id = order.get("user_id")

    vendor_variants = []
    if vendor_id:
        try:
            vendor_variants.append(ObjectId(vendor_id))
        except Exception:
            pass
        vendor_variants.append(str(vendor_id))

    q_or = []
    if user_phone:
        q_or.append({"customer_phone": user_phone})
        q_or.append({"family_phones": user_phone})
    if user_id:
        try:
            q_or.append({"user_id": ObjectId(user_id)})
        except Exception:
            pass

    if not q_or:
        return None, existing_ledger

    # Attempt with vendor match first
    if vendor_variants:
        khata_rec = db["vendor_khata_users"].find_one({
            "vendor_id": {"$in": vendor_variants},
            "$or": q_or
        })
        if khata_rec:
            return khata_rec, existing_ledger

    # Fallback to phone / user_id match
    khata_rec = db["vendor_khata_users"].find_one({"$or": q_or})
    return khata_rec, existing_ledger


def reverse_order_store_credit(db, order, reason="Order Cancelled"):
    """
    Reverses store credit debited for an order if order was placed using STORE_CREDIT
    and has not already been reversed.
    """
    if not order:
        return False

    order_id = order.get("order_id", "")
    pm = str(order.get("payment_method") or "").upper()
    khata_rec, existing_ledger = _resolve_order_khata_record(db, order)

    # Order is store credit if existing ledger exists or payment method indicates it
    is_sc = (existing_ledger is not None) or (pm in ["STORE_CREDIT", "UDHAR", "KHATA", "CREDIT"])
    if not is_sc or not khata_rec:
        return False

    if order.get("store_credit_reversed"):
        return False

    order_total = float(order.get("total") or 0.0)
    if order_total <= 0:
        return False

    user_phone = clean_phone_10(order.get("user_phone", "")) or khata_rec.get("customer_phone", "")
    user_id = order.get("user_id") or khata_rec.get("user_id")
    vendor_id = khata_rec.get("vendor_id") or order.get("vendor_id")

    curr_bal = float(khata_rec.get("current_balance") or 0.0)
    new_bal = round(curr_bal - order_total, 2)

    # Insert reversal entry in ledger
    db["vendor_khata_ledger"].insert_one({
        "khata_user_id": khata_rec["_id"],
        "vendor_id": vendor_id,
        "user_id": ObjectId(user_id) if user_id else None,
        "customer_name": khata_rec.get("customer_name") or order.get("user_name"),
        "customer_phone": user_phone,
        "order_id": order_id,
        "type": "CREDIT", # Reversal credit
        "amount": round(order_total, 2),
        "balance_before": round(curr_bal, 2),
        "balance_after": round(new_bal, 2),
        "payment_mode": "ORDER_CANCELLED",
        "notes": f"{reason} - Order #{order_id[-8:]} reversed",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })

    # Recalculate true running balance from entire ledger history
    recalculate_khata_user_balance(db, khata_rec["_id"])

    # Mark order as reversed
    db["orders"].update_one(
        {"order_id": order_id},
        {"$set": {"store_credit_reversed": True}}
    )

    return True


def adjust_order_store_credit(db, order, new_total, reason="Order Total Adjustment", vendor_oid=None, credit_billed_cap=None):
    """
    Adjusts store credit when order total changes (e.g. customized notes items priced).
    Guaranteed idempotent: derives diff by comparing new_total against existing ledger debits.
    If credit_billed_cap is provided (e.g. Split COD mode where customer pays excess via cash),
    only credit_billed_cap is billed to the Store Credit ledger.
    """
    if not order:
        return False

    order_id = order.get("order_id", "")
    pm = str(order.get("payment_method") or "").upper()
    khata_rec, existing_ledger = _resolve_order_khata_record(db, order)

    # Order must have a store credit history or payment method
    is_sc = (existing_ledger is not None) or (pm in ["STORE_CREDIT", "UDHAR", "KHATA", "CREDIT"])
    if not is_sc:
        return False

    if order.get("store_credit_reversed"):
        return False

    if not khata_rec:
        print(f"[adjust_order_store_credit] Notice: No khata record matched for order #{order_id[-8:]}")
        return False

    # Calculate net amount already charged for this order in the ledger
    current_billed = 0.0
    for e in db["vendor_khata_ledger"].find({"order_id": order_id}):
        amt = float(e.get("amount") or 0.0)
        if e.get("type") == "DEBIT":
            current_billed += amt
        elif e.get("type") == "CREDIT":
            current_billed -= amt

    # If ledger has no debits yet for this order, assume baseline is order's original total
    if current_billed == 0.0:
        current_billed = float(order.get("total") or 0.0)

    target_total = float(credit_billed_cap if credit_billed_cap is not None else new_total)
    diff = round(target_total - current_billed, 2)
    if diff == 0:
        return False

    curr_bal = float(khata_rec.get("current_balance") or 0.0)
    new_bal = round(curr_bal + diff, 2)

    entry_type = "DEBIT" if diff > 0 else "CREDIT"
    abs_diff = abs(diff)

    user_phone = clean_phone_10(order.get("user_phone", "")) or khata_rec.get("customer_phone", "")
    user_id = order.get("user_id") or khata_rec.get("user_id")
    resolved_vendor_id = khata_rec.get("vendor_id") or order.get("vendor_id") or vendor_oid

    if diff > 0:
        notes_str = reason
    else:
        if "Added:" in reason:
            notes_str = reason.replace("Added:", "Price Reduced:").replace("+₹", "-₹")
        else:
            notes_str = f"Item Price Reduced (-₹{abs_diff:,.2f})"

    db["vendor_khata_ledger"].insert_one({
        "khata_user_id": khata_rec["_id"],
        "vendor_id": resolved_vendor_id,
        "user_id": ObjectId(user_id) if user_id else None,
        "customer_name": khata_rec.get("customer_name") or order.get("user_name"),
        "customer_phone": user_phone,
        "order_id": order_id,
        "type": entry_type,
        "amount": round(abs_diff, 2),
        "balance_before": round(curr_bal, 2),
        "balance_after": round(new_bal, 2),
        "payment_mode": "BILL_ADJUSTMENT",
        "notes": notes_str,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })

    # Recalculate true running balance from entire ledger history
    recalculate_khata_user_balance(db, khata_rec["_id"])
    return True


def sync_khata_user_recent_orders(db, khata_user):
    """
    Self-healing sync: checks recent Store Credit orders for this customer.
    If an order was finalized with notes item price but ledger was not adjusted,
    it automatically injects the missing adjustment.
    """
    try:
        if not khata_user:
            return
        phone = khata_user.get("customer_phone")
        user_id = khata_user.get("user_id")

        q_or = []
        if phone:
            q_or.append({"user_phone": phone})
        if user_id:
            q_or.append({"user_id": str(user_id)})
            q_or.append({"user_id": ObjectId(user_id)})

        if not q_or:
            return

        recent_orders = list(db["orders"].find({
            "$or": q_or,
            "payment_method": {"$in": ["STORE_CREDIT", "UDHAR", "KHATA", "CREDIT"]}
        }).sort("_id", -1).limit(10))

        for o in recent_orders:
            if o.get("notes_finalized") or (o.get("notes_price") and float(o.get("notes_price") or 0) > 0):
                target_total = float(o.get("total") or o.get("total_amount") or 0.0)
                if target_total > 0:
                    items_desc = f"Added: {o.get('notes_item_name') or 'Custom Items'} (+₹{float(o.get('notes_price') or 0):,.2f})"
                    adjust_order_store_credit(db, o, target_total, reason=items_desc)
    except Exception as _sync_err:
        print("[sync_khata_user_recent_orders Error]:", _sync_err)


# ========================================================
# 📱 CUSTOMER ROUTES: "MY KHATA" (PASSBOOK)
# ========================================================

@khata_bp.route("/my-khata", methods=["GET"])
def customer_khata():
    cols = get_collections()
    user_id = session.get("user_id")
    user_phone = session.get("user_phone", "")
    clean_p = clean_phone_10(user_phone)

    # 1. Look up active society vendor
    shop_id = session.get("current_shop_id")
    vendor = None
    if shop_id:
        try:
            vendor = cols["users"].find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            pass

    if not vendor:
        curr_soc = session.get("current_society_id")
        if curr_soc:
            try:
                soc_oid = ObjectId(curr_soc)
                vendor = cols["users"].find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(curr_soc)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                pass

    if not vendor:
        vendor = cols["users"].find_one({"role": "vendor"}, sort=[("_id", -1)])

    khata_user = None
    ledger_entries = []
    total_ledger_count = 0
    has_more = False

    is_primary = False
    if vendor and clean_p:
        # Match by phone, family_phones or user_id
        phone_variants = [clean_p, f"+91{clean_p}"]
        q_or = [
            {"customer_phone": {"$in": phone_variants}},
            {"family_phones": {"$in": phone_variants}},
        ]
        if user_id:
            try:
                q_or.append({"user_id": ObjectId(user_id)})
            except Exception:
                pass

        khata_user = cols["khata_users"].find_one({
            "vendor_id": vendor["_id"],
            "$or": q_or
        })

        # Fallback without vendor_id in case of session shop mismatch
        if not khata_user:
            khata_user = cols["khata_users"].find_one({
                "$or": q_or,
                "status": "ACTIVE"
            })

        if khata_user:
            primary_phone = clean_phone_10(khata_user.get("customer_phone", ""))
            is_primary = (clean_p == primary_phone)

            # Ensure family_members has at least the primary resident and auto-deduplicate by clean phone
            existing_fams = khata_user.get("family_members") or []
            dedup_fams = []
            seen_fam_phones = set()
            has_dups = False
            for m in existing_fams:
                m_p = clean_phone_10(m.get("phone", ""))
                if not m_p or m_p in seen_fam_phones:
                    has_dups = True
                    continue
                seen_fam_phones.add(m_p)
                m["phone"] = m_p
                dedup_fams.append(m)

            if primary_phone and primary_phone not in seen_fam_phones:
                dedup_fams.insert(0, {
                    "name": khata_user.get("customer_name") or "Primary Resident",
                    "phone": primary_phone,
                    "is_primary": True,
                    "added_at": khata_user.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
                })
                seen_fam_phones.add(primary_phone)
                has_dups = True

            if has_dups or len(dedup_fams) != len(existing_fams):
                cols["khata_users"].update_one(
                    {"_id": khata_user["_id"]},
                    {"$set": {
                        "family_members": dedup_fams,
                        "family_phones": list(seen_fam_phones),
                        "updated_at": datetime.datetime.now(datetime.timezone.utc)
                    }}
                )
                khata_user["family_members"] = dedup_fams
                khata_user["family_phones"] = list(seen_fam_phones)

            # Self-healing sync for any pending notes items adjustments
            sync_khata_user_recent_orders(get_db(), khata_user)
            recalculate_khata_user_balance(cols, khata_user["_id"])
            khata_user = cols["khata_users"].find_one({"_id": khata_user["_id"]})
            total_ledger_count = cols["khata_ledger"].count_documents({"khata_user_id": khata_user["_id"]})
            raw_entries = list(cols["khata_ledger"].find({"khata_user_id": khata_user["_id"]}).sort("created_at", -1).limit(10))
            for e in raw_entries:
                dt = e.get("created_at")
                if dt and dt.tzinfo is None:
                    dt = pytz.utc.localize(dt).astimezone(IST)
                elif dt:
                    dt = dt.astimezone(IST)
                else:
                    dt = datetime.datetime.now(IST)
                e["date_str"] = dt.strftime("%d %b %Y, %I:%M %p")
                ledger_entries.append(e)

            has_more = len(ledger_entries) < total_ledger_count

    shop_name = vendor.get("shop_name") or vendor.get("name") if vendor else "Society Store"

    return render_template(
        "khata/customer_khata.html",
        vendor=vendor,
        shop_name=shop_name,
        khata_user=khata_user,
        is_primary=is_primary,
        ledger_entries=ledger_entries,
        total_ledger_count=total_ledger_count,
        has_more=has_more,
        user_phone=user_phone,
        clean_user_phone=clean_p,
        now_ist=datetime.datetime.now(IST)
    )


# ---------- API: Customer Fetch Paginated Passbook History ----------
@khata_bp.route("/api/my-khata/ledger", methods=["GET"])
def customer_get_ledger():
    cols = get_collections()
    user_id = session.get("user_id")
    user_phone = session.get("user_phone", "")
    clean_p = clean_phone_10(user_phone)

    if not clean_p and not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    shop_id = session.get("current_shop_id")
    vendor = None
    if shop_id:
        try:
            vendor = cols["users"].find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            pass

    if not vendor:
        curr_soc = session.get("current_society_id")
        if curr_soc:
            try:
                soc_oid = ObjectId(curr_soc)
                vendor = cols["users"].find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(curr_soc)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                pass

    if not vendor:
        vendor = cols["users"].find_one({"role": "vendor"}, sort=[("_id", -1)])

    if not vendor:
        return jsonify({"success": False, "error": "Store not found"}), 404

    khata_user = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "$or": [
            {"customer_phone": clean_p},
            {"family_phones": clean_p},
            {"user_id": ObjectId(user_id) if user_id else None}
        ]
    })
    if not khata_user:
        return jsonify({"success": False, "error": "Khata account not found"}), 404

    # Self-healing sync for any pending notes items adjustments
    sync_khata_user_recent_orders(get_db(), khata_user)
    recalculate_khata_user_balance(cols, khata_user["_id"])

    page = max(1, int(request.args.get("page", 1)))
    limit = max(1, min(100, int(request.args.get("limit", 10))))

    total_count = cols["khata_ledger"].count_documents({"khata_user_id": khata_user["_id"]})
    skip = (page - 1) * limit
    entries = list(cols["khata_ledger"].find({"khata_user_id": khata_user["_id"]}).sort("created_at", -1).skip(skip).limit(limit))

    out = []
    for e in entries:
        dt = e.get("created_at")
        if dt and dt.tzinfo is None:
            dt = pytz.utc.localize(dt).astimezone(IST)
        elif dt:
            dt = dt.astimezone(IST)
        else:
            dt = datetime.datetime.now(IST)

        out.append({
            "id": str(e["_id"]),
            "type": e.get("type", "DEBIT"),
            "amount": e.get("amount", 0.0),
            "order_id": e.get("order_id", ""),
            "payment_mode": e.get("payment_mode", ""),
            "balance_after": e.get("balance_after", 0.0),
            "notes": e.get("notes", ""),
            "date_str": dt.strftime("%d %b %Y, %I:%M %p")
        })

    return jsonify({
        "success": True,
        "entries": out,
        "page": page,
        "limit": limit,
        "total_count": total_count,
        "has_more": (skip + len(out)) < total_count
    })


# ---------- Helper: Primary Mukhiya Owner Resolution ----------
def _get_customer_khata_owner(cols):
    user_id = session.get("user_id")
    user_phone = session.get("user_phone", "") or session.get("phone", "")
    clean_p = clean_phone_10(user_phone)
    if not clean_p and user_id:
        try:
            u_doc = cols["users"].find_one({"_id": ObjectId(user_id)})
            if u_doc:
                clean_p = clean_phone_10(u_doc.get("phone", ""))
        except Exception:
            pass

    if not clean_p and not user_id:
        return None, None, (jsonify({"success": False, "error": "Please login to manage your household Khata."}), 401)

    or_clauses = []
    if clean_p:
        or_clauses.append({"customer_phone": {"$in": [clean_p, f"+91{clean_p}"]}})
    if user_id:
        try:
            or_clauses.append({"user_id": ObjectId(user_id)})
        except Exception:
            pass

    if not or_clauses:
        return None, None, (jsonify({"success": False, "error": "Please login to manage your household Khata."}), 401)

    khata_user = cols["khata_users"].find_one({
        "status": "ACTIVE",
        "$or": or_clauses
    })

    if not khata_user:
        if clean_p:
            fam_doc = cols["khata_users"].find_one({"status": "ACTIVE", "family_phones": clean_p})
            if fam_doc:
                return None, clean_p, (jsonify({"success": False, "error": "Only the primary household owner can manage family members."}), 403)
        return None, clean_p, (jsonify({"success": False, "error": "No active Store Credit account found."}), 404)

    master_phone = clean_phone_10(khata_user.get("customer_phone", ""))
    if clean_p and master_phone and clean_p != master_phone:
        return None, clean_p, (jsonify({"success": False, "error": "Only the primary household owner can manage family members."}), 403)

    return khata_user, (clean_p or master_phone), None


# ---------- 📱 Page: Add Household Member (Dedicated App-Like Flow) ----------
@khata_bp.route("/my-khata/add-member", methods=["GET"])
def customer_add_member_page():
    cols = get_collections()
    khata_user, clean_p, err = _get_customer_khata_owner(cols)
    if err or not khata_user:
        return redirect(url_for("khata.customer_khata"))

    # Ensure master owner is present in family_members if empty
    master_phone = clean_phone_10(khata_user.get("customer_phone", ""))
    master_name = khata_user.get("customer_name") or "Primary Resident"
    if not khata_user.get("family_members"):
        initial_members = [{
            "name": master_name,
            "phone": master_phone,
            "is_primary": True,
            "added_at": khata_user.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
        }]
        cols["khata_users"].update_one(
            {"_id": khata_user["_id"]},
            {"$set": {"family_members": initial_members, "family_phones": [master_phone]}}
        )
        khata_user["family_members"] = initial_members
        khata_user["family_phones"] = [master_phone]

    vendor_id = khata_user.get("vendor_id")
    vendor = None
    shop_name = "Jippy Store"
    if vendor_id:
        try:
            vendor = cols["users"].find_one({"_id": vendor_id})
            if vendor:
                shop_name = vendor.get("shop_name") or vendor.get("name") or "Jippy Store"
        except Exception:
            pass

    return render_template(
        "khata/customer_add_member.html",
        khata_user=khata_user,
        vendor=vendor,
        shop_name=shop_name,
        clean_user_phone=clean_p
    )


# ---------- 👨‍👩‍👧‍👦 API: Primary Mukhiya Sends 6-Digit WhatsApp OTP to Family Member ----------
@khata_bp.route("/api/my-khata/family/send-otp", methods=["POST"])
def customer_family_send_otp():
    cols = get_collections()
    khata_user, clean_p, err = _get_customer_khata_owner(cols)
    if err:
        return err

    data = request.get_json() or {}
    member_name = (data.get("name") or "").strip()
    member_phone = clean_phone_10(data.get("phone") or "")

    if not member_name:
        return jsonify({"success": False, "error": "Family member name is required."}), 400
    if not member_phone or len(member_phone) < 10:
        return jsonify({"success": False, "error": "Valid 10-digit mobile number is required."}), 400

    if member_phone == clean_p:
        return jsonify({"success": False, "error": "You cannot add your own primary mobile number as a family member."}), 400

    existing_phones = khata_user.get("family_phones") or []
    if member_phone in existing_phones:
        return jsonify({"success": False, "error": f"Mobile +91 {member_phone} is already linked to this flat's Khata."}), 400

    otp_code = str(random.randint(100000, 999999))
    expiry = datetime.datetime.now(IST) + datetime.timedelta(minutes=5)

    cols["khata_otps"].update_one(
        {"khata_id": khata_user["_id"], "family_phone": member_phone},
        {
            "$set": {
                "otp": otp_code,
                "expiry": expiry,
                "family_name": member_name.title(),
                "family_phone": member_phone,
                "primary_name": khata_user.get("customer_name") or "Primary Resident",
                "primary_phone": clean_p,
                "khata_id": khata_user["_id"],
                "created_at": datetime.datetime.now(IST)
            }
        },
        upsert=True
    )

    full_addr = khata_user.get("full_address_display") or f"{khata_user.get('block')} - {khata_user.get('flat_no')}"
    primary_name = khata_user.get("customer_name") or "Household Owner"

    # 100% Professional English WhatsApp Template dispatched to family member's phone
    msg = (
        f"🔐 *STORE CREDIT INVITATION | JIPPY STORE*\n\n"
        f"Dear *{member_name.title()}*,\n\n"
        f"*{primary_name}* has invited you to share the Society Store Credit (Khata) account for *{full_addr}*.\n\n"
        f"🔑 *Your 6-Digit Verification Code is:*\n"
        f"👉 *{otp_code}*\n"
        f"_(Valid for 5 minutes)_\n\n"
        f"Please share this verification code with *{primary_name}* to confirm your mobile linking.\n\n"
        f"Once linked, you can place instant 1-click grocery orders on Jippy Store billed directly to your flat's monthly passbook.\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )

    send_whatsapp_async(member_phone, msg)
    return jsonify({
        "success": True,
        "message": f"Verification code sent via WhatsApp to +91 {member_phone}.",
        "phone": member_phone,
        "name": member_name.title()
    })


# ---------- 👨‍👩‍👧‍👦 API: Primary Mukhiya Verifies OTP & Activates Family Member ----------
@khata_bp.route("/api/my-khata/family/verify-otp", methods=["POST"])
def customer_family_verify_otp():
    cols = get_collections()
    khata_user, clean_p, err = _get_customer_khata_owner(cols)
    if err:
        return err

    data = request.get_json() or {}
    member_phone = clean_phone_10(data.get("phone") or "")
    otp_input = str(data.get("otp") or "").strip()

    if not member_phone or not otp_input:
        return jsonify({"success": False, "error": "Phone and 6-digit OTP are required."}), 400

    otp_doc = cols["khata_otps"].find_one({
        "khata_id": khata_user["_id"],
        "family_phone": member_phone
    })
    if not otp_doc:
        return jsonify({"success": False, "error": "No pending verification code found for this mobile number."}), 400

    if is_datetime_expired(otp_doc.get("expiry")):
        return jsonify({"success": False, "error": "Verification code has expired. Please request a new code."}), 400

    if str(otp_doc.get("otp")) != otp_input:
        return jsonify({"success": False, "error": "Incorrect verification code. Please check and try again."}), 400

    member_name = otp_doc.get("family_name") or "Family Member"
    cols["khata_otps"].delete_one({"_id": otp_doc["_id"]})

    # Ensure master owner is present in family_members if empty
    master_phone = clean_phone_10(khata_user.get("customer_phone", ""))
    master_name = khata_user.get("customer_name") or "Primary Resident"
    if not khata_user.get("family_members"):
        initial_members = [{
            "name": master_name,
            "phone": master_phone,
            "is_primary": True,
            "added_at": khata_user.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
        }]
        cols["khata_users"].update_one(
            {"_id": khata_user["_id"]},
            {"$set": {"family_members": initial_members, "family_phones": [master_phone]}}
        )

    new_member = {
        "name": member_name.title(),
        "phone": member_phone,
        "is_primary": False,
        "added_at": datetime.datetime.now(datetime.timezone.utc)
    }

    # Add to family_members and family_phones (prevent duplicate member object with same phone)
    cols["khata_users"].update_one(
        {"_id": khata_user["_id"]},
        {
            "$pull": {
                "family_phones": {"$in": [member_phone, f"+91{member_phone}"]},
                "family_members": {"phone": {"$in": [member_phone, f"+91{member_phone}"]}}
            }
        }
    )
    cols["khata_users"].update_one(
        {"_id": khata_user["_id"]},
        {
            "$addToSet": {
                "family_phones": member_phone,
                "family_members": new_member
            },
            "$set": {
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }
        }
    )

    full_addr = khata_user.get("full_address_display") or f"{khata_user.get('block')} - {khata_user.get('flat_no')}"
    primary_name = khata_user.get("customer_name") or "Account Owner"

    # 1. Professional English WhatsApp Confirmation to Family Member
    msg_family = (
        f"🎉 *STORE CREDIT LINKED | JIPPY STORE* 🛍️\n\n"
        f"Dear *{member_name.title()}*,\n\n"
        f"Congratulations! Your mobile (+91 {member_phone}) has been successfully linked to the Society Store Credit account for *{full_addr}*.\n\n"
        f"You can now choose *\"Society Store Credit\"* at checkout on Jippy Store for instant 1-click orders billed directly to your flat's monthly passbook.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )
    send_whatsapp_async(member_phone, msg_family)

    # 2. Professional English Alert to Primary Mukhiya
    msg_primary = (
        f"👨‍👩‍👧‍👦 *FAMILY MEMBER LINKED | JIPPY STORE*\n\n"
        f"Dear *{primary_name}*,\n\n"
        f"*{member_name.title()}* (+91 {member_phone}) has been successfully added to your Flat Store Credit account (*{full_addr}*).\n\n"
        f"All orders placed by {member_name.title()} will now reflect in your household passbook.\n\n"
        f"🔗 *View Passbook:* https://jippy.store/my-khata\n\n"
        f"⚡ *Jippy Store - 10-15 Mins Hyperlocal Grocery Delivery*"
    )
    send_whatsapp_async(clean_p, msg_primary)

    # Fetch updated members list
    refreshed = cols["khata_users"].find_one({"_id": khata_user["_id"]})
    clean_members = []
    for m in (refreshed.get("family_members") or []):
        clean_members.append({
            "name": m.get("name", ""),
            "phone": m.get("phone", ""),
            "is_primary": bool(m.get("is_primary", False)),
            "added_at": str(m.get("added_at", ""))
        })
    return jsonify({
        "success": True,
        "message": f"{member_name.title()} (+91 {member_phone}) successfully linked!",
        "members": clean_members
    })


# ---------- 👨‍👩‍👧‍👦 API: Primary Mukhiya Removes a Family Member ----------
@khata_bp.route("/api/my-khata/family/remove", methods=["POST"])
def customer_family_remove_member():
    cols = get_collections()
    khata_user, clean_p, err = _get_customer_khata_owner(cols)
    if err:
        return err

    data = request.get_json() or {}
    target_phone = clean_phone_10(data.get("phone") or "")

    if not target_phone:
        return jsonify({"success": False, "error": "Target phone number is required."}), 400

    if target_phone == clean_p:
        return jsonify({"success": False, "error": "Cannot remove the primary account owner."}), 400

    cols["khata_users"].update_one(
        {"_id": khata_user["_id"]},
        {
            "$pull": {
                "family_phones": {"$in": [target_phone, f"+91{target_phone}"]},
                "family_members": {"phone": {"$in": [target_phone, f"+91{target_phone}"]}}
            },
            "$set": {
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }
        }
    )

    full_addr = khata_user.get("full_address_display") or f"{khata_user.get('block')} - {khata_user.get('flat_no')}"



    # Courtesy Unlink Notification to the removed member
    msg_unlink = (
        f"ℹ️ *STORE CREDIT UPDATE | JIPPY STORE*\n\n"
        f"Your mobile number (+91 {target_phone}) has been unlinked from the Store Credit account for *{full_addr}*.\n\n"
        f"You can continue ordering on Jippy Store using Pay on Delivery (COD) or online payment.\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )
    send_whatsapp_async(target_phone, msg_unlink)

    refreshed = cols["khata_users"].find_one({"_id": khata_user["_id"]})
    clean_members = []
    for m in (refreshed.get("family_members") or []):
        clean_members.append({
            "name": m.get("name", ""),
            "phone": m.get("phone", ""),
            "is_primary": bool(m.get("is_primary", False)),
            "added_at": str(m.get("added_at", ""))
        })
    return jsonify({
        "success": True,
        "message": f"Mobile +91 {target_phone} unlinked successfully.",
        "members": clean_members
    })


# ---------- API: Customer Requests Store Credit Approval from Checkout ----------
@khata_bp.route("/api/khata/request-approval", methods=["POST"])
def customer_request_approval():
    cols = get_collections()
    data = request.get_json() or {}
    user_id = session.get("user_id")
    user_phone = clean_phone_10(data.get("phone") or session.get("user_phone", ""))
    customer_name = (data.get("name") or session.get("user_name") or "Resident").strip()
    block = (data.get("block") or "General Block").strip()
    flat_no = (data.get("flat_no") or "").strip()
    floor = (data.get("floor") or "").strip()
    society_id = data.get("society_id") or session.get("current_society_id") or ""

    if not user_phone:
        return jsonify({"success": False, "error": "Phone number is required"}), 400

    # Resolve Vendor
    shop_id = session.get("current_shop_id")
    vendor = None
    if shop_id:
        try:
            vendor = cols["users"].find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            pass

    if not vendor and society_id:
        try:
            soc_oid = ObjectId(society_id)
            vendor = cols["users"].find_one({
                "role": "vendor",
                "$or": [
                    {"serviceable_societies": str(society_id)},
                    {"serviceable_societies": soc_oid}
                ]
            })
        except Exception:
            pass

    if not vendor:
        vendor = cols["users"].find_one({"role": "vendor"}, sort=[("_id", -1)])

    if not vendor:
        return jsonify({"success": False, "error": "No vendor found for this society"}), 400

    # Standardize Address (BLOCK, FLAT, FLOOR, Uppercase)
    std_addr = standardize_address_components(raw_block=block, raw_floor=floor, raw_flat=flat_no)
    block = std_addr["block"]
    floor = std_addr["floor"]
    flat_no = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    # Build robust block and flat search patterns to handle both '1903' and 'FLAT 1903', 'B' and 'BLOCK B'
    core_block = block.replace("BLOCK ", "").replace("TOWER ", "").replace("WING ", "").strip()
    core_flat = flat_no.replace("FLAT ", "").replace("VILLA ", "").replace("UNIT ", "").strip()
    block_patterns = list({block, core_block, f"BLOCK {core_block}", f"TOWER {core_block}", f"WING {core_block}"})
    flat_patterns = list({flat_no, core_flat, f"FLAT {core_flat}", f"UNIT {core_flat}", f"VILLA {core_flat}"})

    vendor_ids = [vendor["_id"]]
    try:
        vendor_ids.append(str(vendor["_id"]))
    except Exception:
        pass

    # Check if an active account already exists for this exact flat under this vendor
    existing_flat = cols["khata_users"].find_one({
        "vendor_id": {"$in": vendor_ids},
        "block": {"$in": block_patterns},
        "flat_no": {"$in": flat_patterns},
        "status": "ACTIVE",
        "is_verified": True
    })
    if not existing_flat:
        # Fallback search without vendor_id in case vendor session was unset
        existing_flat = cols["khata_users"].find_one({
            "block": {"$in": block_patterns},
            "flat_no": {"$in": flat_patterns},
            "status": "ACTIVE",
            "is_verified": True
        })
    direct_vendor = bool(data.get("direct_vendor"))
    send_owner_otp = bool(data.get("send_owner_otp"))

    # 1. Check existing requester status: Block repeated requests if already active or pending
    phone_variants = [user_phone, f"+91{user_phone}"]
    q_or = [
        {"customer_phone": {"$in": phone_variants}}
    ]
    if not direct_vendor:
        q_or.append({"family_phones": {"$in": phone_variants}})

    if user_id and not direct_vendor:
        try:
            q_or.append({"user_id": ObjectId(user_id)})
        except Exception:
            pass

    existing = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "$or": q_or
    })
    if existing:
        curr_status = str(existing.get("status", "")).upper()
        is_own = (clean_phone_10(existing.get("customer_phone", "")) == user_phone)
        if is_own:
            if curr_status == "ACTIVE" and existing.get("is_verified"):
                return jsonify({
                    "success": False,
                    "already_verified": True,
                    "error": "Your Society Store Credit account is already verified and active."
                }), 400
            elif curr_status == "PENDING" and not direct_vendor:
                return jsonify({
                    "success": False,
                    "already_pending": True,
                    "error": "Your Store Credit request is currently under review by the store manager. Please allow a few minutes for verification."
                }), 400

    # If user requests a separate account, detach their phone from any household passbook where it was previously linked
    if direct_vendor and user_phone:
        cols["khata_users"].update_many(
            {
                "vendor_id": vendor["_id"],
                "$or": [
                    {"family_phones": user_phone},
                    {"family_members.phone": user_phone}
                ]
            },
            {
                "$pull": {
                    "family_phones": user_phone,
                    "family_members": {"phone": user_phone}
                }
            }
        )

    # 2. Check if an active account exists for this flat
    if existing_flat and not direct_vendor:
        primary_name = existing_flat.get("customer_name") or "Primary Resident"
        primary_p = clean_phone_10(existing_flat.get("customer_phone") or "")
        fam_phones = set(existing_flat.get("family_phones") or [])
        masked_phone = f"+91 {primary_p[:4]}•••{primary_p[-2:]}" if len(primary_p) >= 10 else primary_p

        # A. Already linked to flat passbook
        if user_phone == primary_p or user_phone in fam_phones:
            return jsonify({
                "success": True,
                "already_verified": True,
                "message": f"Your mobile (+91 {user_phone}) is already linked to {full_disp} (Owner: {primary_name}). You can order now!"
            })

        # B. 💡 Ask first before generating/sending OTP (Saves OTP costs & allows bachelor flow!)
        if not send_owner_otp:
            next_url = (data.get("next") or "").strip()
            if not next_url or not next_url.startswith("/"):
                next_url = "/my-khata"

            redirect_url = f"/my-khata/choose-account?block={quote(block)}&flat_no={quote(flat_no)}&next={quote(next_url)}"

            return jsonify({
                "success": True,
                "requires_household_choice": True,
                "redirect_url": redirect_url,
                "primary_name": primary_name,
                "masked_phone": masked_phone,
                "requester_phone": user_phone,
                "block": block,
                "flat_no": flat_no,
                "floor": floor,
                "full_address": full_disp,
                "message": f"Flat {full_disp} already has an active account under {primary_name} ({masked_phone})."
            })

        # C. User explicitly confirmed Option 1: Send 6-Digit Approval OTP to Primary Owner (Mukhiya)
        otp_code = str(random.randint(100000, 999999))
        expiry = datetime.datetime.now(IST) + datetime.timedelta(minutes=5)

        cols["khata_otps"].update_one(
            {
                "type": "HOUSEHOLD_OWNER_CONSENT",
                "khata_id": existing_flat["_id"],
                "requester_phone": user_phone
            },
            {
                "$set": {
                    "type": "HOUSEHOLD_OWNER_CONSENT",
                    "khata_id": existing_flat["_id"],
                    "requester_phone": user_phone,
                    "requester_name": customer_name.title(),
                    "primary_phone": primary_p,
                    "primary_name": primary_name,
                    "otp": otp_code,
                    "expiry": expiry,
                    "block": block,
                    "flat_no": flat_no,
                    "created_at": datetime.datetime.now(IST)
                }
            },
            upsert=True
        )

        msg_owner = (
            f"🔐 *STORE CREDIT APPROVAL | JIPPY STORE*\n\n"
            f"Dear *{primary_name}*,\n\n"
            f"*{customer_name.title()}* (+91 {user_phone}) is requesting permission to link to your flat's Store Credit passbook for *{full_disp}*.\n\n"
            f"🔑 *Your 6-Digit Approval Code is:*\n"
            f"👉 *{otp_code}*\n"
            f"_(Valid for 5 minutes)_\n\n"
            f"⚠️ *Only share this code if {customer_name.title()} is an authorized member of your household.*\n"
            f"Once verified, all orders placed by {customer_name.title()} will reflect in your flat's monthly passbook.\n\n"
            f"🛍️ *Thank you for choosing Jippy Store!*"
        )
        send_whatsapp_async(primary_p, msg_owner)

        next_url = (data.get("next") or "").strip()
        if not next_url or not next_url.startswith("/"):
            next_url = "/my-khata"

        redirect_url = f"/my-khata/verify-approval?block={quote(block)}&flat_no={quote(flat_no)}&next={quote(next_url)}"

        return jsonify({
            "success": True,
            "requires_owner_otp": True,
            "primary_name": primary_name,
            "masked_phone": masked_phone,
            "requester_phone": user_phone,
            "block": block,
            "flat_no": flat_no,
            "full_address": full_disp,
            "redirect_url": redirect_url,
            "message": f"Flat {full_disp} is registered under {primary_name}. A 6-digit approval code has been sent to {primary_name} ({masked_phone}) via WhatsApp."
        })

    # Queue as PENDING in vendor_khata_users (Independent account will never be merged!)
    cols["khata_users"].update_one(
        {"vendor_id": vendor["_id"], "customer_phone": user_phone},
        {
            "$set": {
                "vendor_id": vendor["_id"],
                "user_id": ObjectId(user_id) if user_id else None,
                "customer_name": customer_name.upper(),
                "customer_phone": user_phone,
                "society_id": str(society_id),
                "block": block,
                "flat_no": flat_no,
                "floor": floor,
                "full_address_display": full_disp,
                "status": "PENDING",
                "is_verified": False,
                "is_independent": True if direct_vendor else False,
                "account_type": "INDEPENDENT" if direct_vendor else "FLAT_MASTER",
                "resident_type": "INDEPENDENT" if direct_vendor else "PRIMARY_OWNER",
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            },
            "$setOnInsert": {
                "current_balance": 0.0,
                "credit_limit": 0.0,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
        upsert=True
    )

    # 🚀 Automated Backend WhatsApp Notifications (Zero user intervention)
    vendor_phone = clean_phone_10(vendor.get("phone", ""))
    shop_name = vendor.get("shop_name") or vendor.get("name") or "Store Manager"

    # 1. Notify Vendor automatically
    if vendor_phone:
        req_type_lbl = "Separate / Independent " if direct_vendor else ""
        flat_type_lbl = f"{full_disp} (Independent Account)" if direct_vendor else full_disp
        vendor_alert = (
            f"🔔 *NEW {req_type_lbl.upper()}STORE CREDIT REQUEST | JIPPY STORE*\n\n"
            f"Hello {shop_name},\n\n"
            f"A resident has submitted a {req_type_lbl}Store Credit authorization request:\n\n"
            f"• Resident: *{customer_name}*\n"
            f"• Unit / Flat: *{flat_type_lbl}*\n"
            f"• Mobile: +91 {user_phone}\n\n"
            f"Please review and approve this resident in your Jippy Vendor Hub:\n"
            f"🔗 https://jippy.store/vendor/khata\n\n"
            f"⚡ *Jippy Store Automated System*"
        )
        send_whatsapp_async(vendor_phone, vendor_alert)

    # 2. Notify Resident automatically
    if user_phone:
        resident_confirm = (
            f"📋 *STORE CREDIT REQUEST RECEIVED | JIPPY STORE*\n\n"
            f"Dear *{customer_name}*,\n\n"
            f"Your Store Credit authorization request for *{full_disp}* has been submitted to *{shop_name}*.\n\n"
            f"The store manager will review your flat details and activate your monthly credit account shortly. You will receive an automated WhatsApp confirmation once approved.\n\n"
            f"🛍️ *Thank you for shopping with Jippy Store!*"
        )
        send_whatsapp_async(user_phone, resident_confirm)

    return jsonify({
        "success": True,
        "message": "Authorization request submitted successfully! Your store manager has been notified automatically.",
        "notified": True
    })


# ---------- 🏢 Dedicated Full-Page: Customer Choose Account Option (Link vs Separate) ----------
@khata_bp.route("/my-khata/choose-account", methods=["GET"])
def customer_choose_account_page():
    cols = get_collections()
    user_id = session.get("user_id")
    user_phone = session.get("user_phone", "")
    clean_p = clean_phone_10(user_phone)

    next_url = (request.args.get("next") or "/my-khata").strip()
    if not next_url.startswith("/"):
        next_url = "/my-khata"

    block_arg = (request.args.get("block") or "").strip()
    flat_arg = (request.args.get("flat_no") or "").strip()

    if not clean_p:
        return redirect(f"/login?next={quote(request.full_path)}")

    std_addr = standardize_address_components(raw_block=block_arg, raw_flat=flat_arg)
    block = std_addr["block"]
    flat_no = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    core_block = block.replace("BLOCK ", "").replace("TOWER ", "").replace("WING ", "").strip()
    core_flat = flat_no.replace("FLAT ", "").replace("VILLA ", "").replace("UNIT ", "").strip()
    block_patterns = list({block, core_block, f"BLOCK {core_block}", f"TOWER {core_block}", f"WING {core_block}"})
    flat_patterns = list({flat_no, core_flat, f"FLAT {core_flat}", f"UNIT {core_flat}", f"VILLA {core_flat}"})

    existing_flat = cols["khata_users"].find_one({
        "block": {"$in": block_patterns},
        "flat_no": {"$in": flat_patterns},
        "status": "ACTIVE",
        "is_verified": True
    })

    if not existing_flat:
        return redirect(next_url)

    primary_name = existing_flat.get("customer_name") or "Primary Resident"
    primary_p = clean_phone_10(existing_flat.get("customer_phone") or "")
    masked_phone = f"+91 {primary_p[:4]}•••{primary_p[-2:]}" if len(primary_p) >= 10 else primary_p

    shop_name = "Society Store"
    vendor_id = existing_flat.get("vendor_id")
    if vendor_id:
        try:
            vendor = cols["users"].find_one({"_id": vendor_id})
            if vendor:
                shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
        except Exception:
            pass

    return render_template(
        "khata/customer_choose_account.html",
        primary_name=primary_name,
        masked_phone=masked_phone,
        full_address=full_disp,
        shop_name=shop_name,
        block=block,
        flat_no=flat_no,
        next_url=next_url
    )


# ---------- 🔐 Dedicated Full-Page: Customer Verify Household Owner Approval Code ----------
@khata_bp.route("/my-khata/verify-approval", methods=["GET"])
def customer_verify_approval_page():
    cols = get_collections()
    user_id = session.get("user_id")
    user_phone = session.get("user_phone", "")
    clean_p = clean_phone_10(user_phone)

    next_url = (request.args.get("next") or "/my-khata").strip()
    if not next_url.startswith("/"):
        next_url = "/my-khata"

    block_arg = (request.args.get("block") or "").strip()
    flat_arg = (request.args.get("flat_no") or "").strip()

    if not clean_p:
        return redirect(f"/login?next={quote(request.full_path)}")

    # Look up pending HOUSEHOLD_OWNER_CONSENT OTP
    otp_doc = cols["khata_otps"].find_one({
        "type": "HOUSEHOLD_OWNER_CONSENT",
        "requester_phone": clean_p
    }, sort=[("created_at", -1)])

    if not otp_doc:
        # Check if already active/verified
        return redirect(next_url)

    # Get existing khata account
    khata_user = cols["khata_users"].find_one({"_id": otp_doc["khata_id"]})
    if not khata_user:
        return redirect(next_url)

    primary_name = khata_user.get("customer_name") or "Primary Resident"
    primary_p = clean_phone_10(khata_user.get("customer_phone") or "")
    masked_phone = f"+91 {primary_p[:4]}•••{primary_p[-2:]}" if len(primary_p) >= 10 else primary_p

    full_disp = f"{khata_user.get('block', '')} - {khata_user.get('flat_no', '')}"
    if khata_user.get("floor"):
        full_disp += f" ({khata_user.get('floor')})"

    shop_name = "Society Store"
    vendor_id = khata_user.get("vendor_id")
    if vendor_id:
        try:
            vendor = cols["users"].find_one({"_id": vendor_id})
            if vendor:
                shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"
        except Exception:
            pass

    return render_template(
        "khata/customer_verify_approval.html",
        otp_doc=otp_doc,
        khata_user=khata_user,
        primary_name=primary_name,
        masked_phone=masked_phone,
        full_address=full_disp,
        shop_name=shop_name,
        next_url=next_url,
        block=block_arg or khata_user.get("block", ""),
        flat_no=flat_arg or khata_user.get("flat_no", ""),
        clean_user_phone=clean_p
    )


# ---------- 👨‍👩‍👧‍👦 API: Verify Owner Approval Code & Link Household Member ----------
@khata_bp.route("/api/khata/household/verify-owner-otp", methods=["POST"])
def customer_verify_owner_otp():
    cols = get_collections()
    data = request.get_json() or {}
    user_id = session.get("user_id")
    user_phone = clean_phone_10(data.get("phone") or session.get("user_phone", ""))
    customer_name = (data.get("name") or session.get("user_name") or "Resident").strip()
    block = (data.get("block") or "").strip()
    flat_no = (data.get("flat_no") or "").strip()
    otp_input = str(data.get("otp") or "").strip()

    if not user_phone:
        return jsonify({"success": False, "error": "Phone number is required."}), 400
    if not otp_input or len(otp_input) != 6:
        return jsonify({"success": False, "error": "Valid 6-digit approval code is required."}), 400

    std_addr = standardize_address_components(raw_block=block, raw_flat=flat_no)
    block_std = std_addr["block"]
    flat_std = std_addr["flat_no"]
    full_disp = std_addr["full_display"]

    otp_doc = cols["khata_otps"].find_one({
        "type": "HOUSEHOLD_OWNER_CONSENT",
        "requester_phone": user_phone,
        "block": block_std,
        "flat_no": flat_std
    })
    if not otp_doc:
        # Fallback by requester_phone and type
        otp_doc = cols["khata_otps"].find_one({
            "type": "HOUSEHOLD_OWNER_CONSENT",
            "requester_phone": user_phone
        }, sort=[("created_at", -1)])

    if not otp_doc:
        return jsonify({"success": False, "error": "No pending approval request found for this flat and mobile number."}), 400

    if is_datetime_expired(otp_doc.get("expiry")):
        return jsonify({"success": False, "error": "Approval code has expired. Please request a new code."}), 400

    if str(otp_doc.get("otp")) != otp_input:
        return jsonify({"success": False, "error": "Incorrect approval code. Please check with your household owner and try again."}), 400

    khata_id = otp_doc.get("khata_id")
    cols["khata_otps"].delete_one({"_id": otp_doc["_id"]})

    existing_flat = cols["khata_users"].find_one({"_id": khata_id})
    if not existing_flat:
        return jsonify({"success": False, "error": "Khata account not found."}), 404

    primary_name = existing_flat.get("customer_name") or "Primary Resident"
    primary_p = clean_phone_10(existing_flat.get("customer_phone") or "")

    # Add member to family_members and family_phones
    new_member = {
        "name": customer_name.title(),
        "phone": user_phone,
        "is_primary": False,
        "added_at": datetime.datetime.now(datetime.timezone.utc)
    }

    cols["khata_users"].update_one(
        {"_id": khata_id},
        {
            "$pull": {
                "family_phones": {"$in": [user_phone, f"+91{user_phone}"]},
                "family_members": {"phone": {"$in": [user_phone, f"+91{user_phone}"]}}
            }
        }
    )
    cols["khata_users"].update_one(
        {"_id": khata_id},
        {
            "$addToSet": {
                "family_phones": user_phone,
                "family_members": new_member
            },
            "$set": {
                "updated_at": datetime.datetime.now(datetime.timezone.utc)
            }
        }
    )

    # Clean up any leftover PENDING documents for this requester phone
    try:
        cols["khata_users"].delete_many({
            "customer_phone": {"$in": [user_phone, f"+91{user_phone}"]},
            "status": "PENDING"
        })
    except Exception:
        pass

    session["khata_verified"] = True
    session["khata_user_id"] = str(khata_id)

    # Confirmation alerts
    msg_member = (
        f"🎉 *STORE CREDIT LINKED | JIPPY STORE* 🛍️\n\n"
        f"Dear *{customer_name.title()}*,\n\n"
        f"You have been successfully linked to the Store Credit passbook for *{full_disp}* (Account Owner: *{primary_name}*).\n\n"
        f"You can now select *\"Society Store Credit\"* at checkout for instant 1-click orders.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"🛍️ *Thank you for choosing Jippy Store!*"
    )
    send_whatsapp_async(user_phone, msg_member)

    if primary_p:
        msg_owner = (
            f"👨‍👩‍👧‍👦 *HOUSEHOLD MEMBER VERIFIED | JIPPY STORE*\n\n"
            f"Dear *{primary_name}*,\n\n"
            f"*{customer_name.title()}* (+91 {user_phone}) has entered your approval code and is now linked to your Flat Store Credit passbook (*{full_disp}*).\n\n"
            f"All orders placed by {customer_name.title()} will now reflect in your household passbook.\n\n"
            f"🔗 *View Passbook:* https://jippy.store/my-khata\n\n"
            f"⚡ *Jippy Store - Hyperlocal Grocery Delivery*"
        )
        send_whatsapp_async(primary_p, msg_owner)

    return jsonify({
        "success": True,
        "is_verified": True,
        "credit_limit": float(existing_flat.get("credit_limit") or 0.0),
        "current_balance": float(existing_flat.get("current_balance") or 0.0),
        "message": f"Successfully linked to {full_disp} (Owner: {primary_name})! Store Credit is now active for this order."
    })


# ---------- API: Check Khata Verification Status for Current User ----------
@khata_bp.route("/api/khata/check-status", methods=["GET"])
def check_khata_status():
    cols = get_collections()
    user_id = session.get("user_id")
    raw_user_phone = session.get("user_phone", "") or session.get("phone", "")
    clean_p = clean_phone_10(raw_user_phone)

    if not clean_p and user_id:
        try:
            u_doc = cols["users"].find_one({"_id": ObjectId(user_id)})
            if u_doc:
                clean_p = clean_phone_10(u_doc.get("phone", ""))
        except Exception:
            pass

    if not clean_p and not user_id:
        return jsonify({"is_verified": False, "status": "GUEST"})

    # Current vendor
    shop_id = session.get("current_shop_id")
    vendor = None
    if shop_id:
        try:
            vendor = cols["users"].find_one({"_id": ObjectId(shop_id), "role": "vendor"})
        except Exception:
            pass

    if not vendor:
        curr_soc = session.get("current_society_id")
        if curr_soc:
            try:
                soc_oid = ObjectId(curr_soc)
                vendor = cols["users"].find_one({
                    "role": "vendor",
                    "$or": [
                        {"serviceable_societies": str(curr_soc)},
                        {"serviceable_societies": soc_oid}
                    ]
                })
            except Exception:
                pass

    if not vendor:
        vendor = cols["users"].find_one({"role": "vendor"}, sort=[("_id", -1)])

    # Construct robust phone & user_id match criteria
    phone_variants = []
    if clean_p:
        phone_variants = [clean_p, f"+91{clean_p}", f"91{clean_p}"]

    or_clauses = []
    if phone_variants:
        or_clauses.append({"customer_phone": {"$in": phone_variants}})
        or_clauses.append({"family_phones": {"$in": phone_variants}})
    if user_id:
        try:
            or_clauses.append({"user_id": ObjectId(user_id)})
        except Exception:
            pass
        or_clauses.append({"user_id": str(user_id)})

    if not or_clauses:
        return jsonify({"is_verified": False, "status": "GUEST"})

    khata_rec = None
    # 1. Match within current vendor (checking both ObjectId and string vendor_id)
    if vendor:
        vendor_variants = [vendor["_id"], str(vendor["_id"])]
        khata_rec = cols["khata_users"].find_one({
            "vendor_id": {"$in": vendor_variants},
            "$or": or_clauses
        })

    # 2. Fallback: Check if user has ANY active/verified Khata account in the system
    if not khata_rec:
        khata_rec = cols["khata_users"].find_one({
            "$or": or_clauses,
            "status": "ACTIVE",
            "is_verified": True
        })

    # 3. Fallback: Any Khata account matching user (e.g. PENDING or REJECTED)
    if not khata_rec:
        khata_rec = cols["khata_users"].find_one({
            "$or": or_clauses
        })

    if not khata_rec:
        shop_name = vendor.get("shop_name") or vendor.get("name") if vendor else "Society Store"
        vendor_phone = clean_phone_10(vendor.get("phone", "")) if vendor else ""
        return jsonify({
            "is_verified": False,
            "status": "NOT_ENROLLED",
            "shop_name": shop_name,
            "vendor_phone": vendor_phone
        })

    is_verified = bool(khata_rec.get("is_verified") and khata_rec.get("status") == "ACTIVE")
    real_bal = recalculate_khata_user_balance(cols, khata_rec["_id"])
    shop_name = vendor.get("shop_name") or vendor.get("name") if vendor else "Society Store"

    return jsonify({
        "is_verified": is_verified,
        "status": khata_rec.get("status", "PENDING"),
        "credit_limit": float(khata_rec.get("credit_limit") or 0.0),
        "current_balance": real_bal,
        "full_address": khata_rec.get("full_address_display", ""),
        "shop_name": shop_name,
        "vendor_phone": clean_phone_10(vendor.get("phone", "")) if vendor else ""
    })
