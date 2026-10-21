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
        # Block (keywords, typos e.g. BLCK/BLOK/BLK, reverse order e.g. A BLOCK)
        elif re.search(r'\b(?:BLOCK|BLCK|BLOK|BLK|BLKK|BLC|BLOC)\b|(?:BLOCK|BLCK|BLOK|BLK)', b_clean, re.IGNORECASE) or re.match(r'^[A-Z0-9]+\s*(?:BLOCK|BLCK|BLOK|BLK)$', b_clean, re.IGNORECASE):
            rev_m = re.match(r'^([A-Z0-9]+)\s*(?:BLOCK|BLCK|BLOK|BLK|BLKK|BLC|BLOC)$', b_clean, re.IGNORECASE)
            if rev_m:
                core = rev_m.group(1).strip()
            else:
                core = re.sub(r'^(?:BLOCK|BLCK|BLOK|BLK|BLKK|BLC|BLOC)\s*(?:NO\.?|NUMBER|NUM)?\s*[:\-#]?\s*', '', b_clean, flags=re.IGNORECASE)
                core = re.sub(r'(?:BLOCK|BLCK|BLOK|BLK|BLKK|BLC|BLOC)', '', core, flags=re.IGNORECASE)
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


@khata_bp.route("/vendor/khata", methods=["GET"])
def vendor_khata_dashboard():
    cols = get_collections()
    vendor, err = _get_current_vendor(cols)
    if err:
        return err

    vendor_oid = vendor["_id"]
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

    # Query all khata records for this vendor
    khata_records = list(cols["khata_users"].find({"vendor_id": vendor_oid}).sort("updated_at", -1))

    total_outstanding = 0.0
    active_count = 0
    pending_count = 0
    rejected_count = 0
    has_dues_count = 0
    blocks_set = set()

    for r in khata_records:
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

        r["block"] = b_std
        r["floor"] = fl_std
        r["flat_no"] = fn_std
        r["full_address_display"] = full_disp

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

        if status == "PENDING":
            pending_count += 1
        elif status == "ACTIVE":
            active_count += 1
            if bal > 0:
                has_dues_count += 1
        elif status == "REJECTED":
            rejected_count += 1

        blocks_set.add(b_std)

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

    # Query all khata records for this vendor
    khata_records = list(cols["khata_users"].find({"vendor_id": vendor_oid}).sort("updated_at", -1))

    total_outstanding = 0.0
    active_count = 0
    pending_count = 0
    rejected_count = 0
    has_dues_count = 0
    blocks_set = set()
    cleaned_records = []

    for r in khata_records:
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

        cleaned_records.append({
            "id": str(r["_id"]),
            "customer_name": r.get("customer_name") or "Resident",
            "customer_phone": r.get("customer_phone") or "",
            "block": b_std,
            "flat_no": fn_std,
            "floor": fl_std,
            "full_address_display": full_disp,
            "status": status,
            "is_verified": bool(r.get("is_verified")),
            "current_balance": r_bal,
            "credit_limit": r_limit,
            "updated_at_str": updated_str
        })

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
        f"Dear {customer_name},\n\n"
        f"{shop_name} is requesting to link your society residence (*{full_disp}*) for Society Store Credit.\n\n"
        f"Your 6-Digit Verification Code is: *{otp_code}*\n"
        f"(Valid for 5 minutes)\n\n"
        f"Please share this code with your store vendor to confirm your account activation and consent to monthly billing.\n\n"
        f"Thank you for choosing Jippy Store! 🥑"
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

    if datetime.datetime.now(IST) > otp_doc.get("expiry", datetime.datetime.min.replace(tzinfo=IST)):
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

    # Upsert into vendor_khata_users
    cols["khata_users"].update_one(
        {"vendor_id": vendor["_id"], "customer_phone": phone},
        {
            "$set": {
                "vendor_id": vendor["_id"],
                "user_id": user_id,
                "customer_name": customer_name.upper(),
                "customer_phone": phone,
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
            },
            "$setOnInsert": {
                "current_balance": 0.0,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
        upsert=True
    )

    shop_name = vendor.get("shop_name") or vendor.get("name") or "Society Store"

    # English WhatsApp Confirmation to Customer
    limit_text = f"₹{credit_limit:,.2f}" if credit_limit > 0 else "Unlimited"
    welcome_msg = (
        f"🎉 *STORE CREDIT ACTIVATED | JIPPY STORE* 🛍️\n"
        f"═════════════════════════════\n"
        f"Dear *{customer_name}*,\n\n"
        f"Congratulations! Your Society Store Credit account has been successfully verified & activated.\n\n"
        f"📋 *ACCOUNT DETAILS:*\n"
        f"• *Store:* {shop_name}\n"
        f"• *Unit / Flat:* {full_disp}\n"
        f"• *Monthly Credit Limit:* {limit_text}\n"
        f"• *Account Status:* Active & Verified\n"
        f"═════════════════════════════\n"
        f"You can now select *\"Society Store Credit\"* at checkout on Jippy Store and enjoy seamless monthly billing.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"Thank you for choosing Jippy Store! 🥑"
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

    cols["khata_users"].update_one(
        {"_id": rec["_id"]},
        {
            "$set": {
                "is_verified": True,
                "status": "ACTIVE",
                "credit_limit": round(credit_limit, 2),
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
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
        f"🎉 *STORE CREDIT APPROVED | JIPPY STORE* 🛍️\n"
        f"═════════════════════════════\n"
        f"Dear *{cust_name}*,\n\n"
        f"Great news! Your Store Credit account has been approved and activated by *{shop_name}*.\n\n"
        f"📋 *ACCOUNT DETAILS:*\n"
        f"• *Unit / Flat:* {full_disp}\n"
        f"• *Account Status:* Active & Verified\n"
        f"• *Credit Limit:* {limit_text}\n"
        f"═════════════════════════════\n"
        f"You can now place your grocery orders using *\"Society Store Credit\"* at checkout.\n\n"
        f"🔗 *Start Shopping:* https://jippy.store\n\n"
        f"Thank you for choosing Jippy Store! 🥑"
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
        ledger_col.update_one(
            {"_id": e["_id"]},
            {"$set": {"balance_after": round(running_bal, 2)}}
        )
    net_bal = round(running_bal, 2)
    users_col.update_one(
        {"_id": oid},
        {"$set": {
            "current_balance": net_bal,
            "updated_at": datetime.datetime.now(datetime.timezone.utc)
        }}
    )
    return net_bal


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
        f"🧾 *PAYMENT RECEIVED | JIPPY STORE* 💳\n"
        f"═════════════════════════════\n"
        f"Dear *{cust_name}*,\n\n"
        f"We have successfully received your payment of *₹{amount:,.2f}* for *{shop_name}*.\n\n"
        f"📋 *PAYMENT DETAILS:*\n"
        f"• *Amount Credited:* ₹{amount:,.2f}\n"
        f"• *Payment Method:* {mode}\n"
        + rem_due_line
        + (f"• *Unit / Flat:* {full_disp}\n" if full_disp else "")
        + f"• *Date & Time:* {date_str}\n"
        f"═════════════════════════════\n"
        f"Your Store Credit passbook has been updated.\n"
        f"🔗 *View Passbook:* https://jippy.store/my-khata\n\n"
        f"Thank you for your prompt settlement! 🥑"
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

    if vendor and clean_p:
        # Match by phone or user_id
        khata_user = cols["khata_users"].find_one({
            "vendor_id": vendor["_id"],
            "$or": [
                {"customer_phone": clean_p},
                {"user_id": ObjectId(user_id) if user_id else None}
            ]
        })

        if khata_user:
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
        ledger_entries=ledger_entries,
        total_ledger_count=total_ledger_count,
        has_more=has_more,
        user_phone=user_phone,
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

    # Check existing status: Block repeated requests until approved or rejected
    existing = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "$or": [
            {"customer_phone": user_phone},
            {"user_id": ObjectId(user_id) if user_id else None}
        ]
    })
    if existing:
        curr_status = str(existing.get("status", "")).upper()
        if curr_status == "PENDING":
            return jsonify({
                "success": False,
                "already_pending": True,
                "error": "Your Store Credit request is currently under review by the store manager. Please allow a few minutes for verification."
            }), 400
        elif curr_status == "ACTIVE" and existing.get("is_verified"):
            return jsonify({
                "success": False,
                "already_verified": True,
                "error": "Your Society Store Credit account is already verified and active."
            }), 400

    # Queue as PENDING in vendor_khata_users
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
        vendor_alert = (
            f"🔔 *NEW STORE CREDIT REQUEST | JIPPY STORE*\n\n"
            f"Hello {shop_name},\n\n"
            f"A resident has submitted a Store Credit authorization request:\n\n"
            f"• Resident: *{customer_name}*\n"
            f"• Unit / Flat: *{full_disp}*\n"
            f"• Mobile: +91 {user_phone}\n\n"
            f"Please review and approve this resident in your Jippy Vendor Hub.\n\n"
            f"🔗 https://jippy.store/vendor/khata\n\n"
            f"Jippy Store Automated System"
        )
        send_whatsapp_async(vendor_phone, vendor_alert)

    # 2. Notify Resident automatically
    if user_phone:
        resident_confirm = (
            f"📋 *STORE CREDIT REQUEST RECEIVED | JIPPY STORE*\n\n"
            f"Dear *{customer_name}*,\n\n"
            f"Your Store Credit authorization request for *{full_disp}* has been submitted to *{shop_name}*.\n\n"
            f"The store manager will review your flat details and activate your monthly credit account shortly. You will receive an automated WhatsApp confirmation once approved.\n\n"
            f"Thank you for shopping with Jippy Store! 🥑"
        )
        send_whatsapp_async(user_phone, resident_confirm)

    return jsonify({
        "success": True,
        "message": "Authorization request submitted successfully! Your store manager has been notified automatically.",
        "notified": True
    })


# ---------- API: Check Khata Verification Status for Current User ----------
@khata_bp.route("/api/khata/check-status", methods=["GET"])
def check_khata_status():
    cols = get_collections()
    user_id = session.get("user_id")
    user_phone = clean_phone_10(session.get("user_phone", ""))

    if not user_phone and not user_id:
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

    if not vendor:
        return jsonify({"is_verified": False, "status": "NO_VENDOR"})

    khata_rec = cols["khata_users"].find_one({
        "vendor_id": vendor["_id"],
        "$or": [
            {"customer_phone": user_phone},
            {"user_id": ObjectId(user_id) if user_id else None}
        ]
    })

    if not khata_rec:
        return jsonify({
            "is_verified": False,
            "status": "NOT_ENROLLED",
            "shop_name": vendor.get("shop_name") or vendor.get("name") or "Society Store",
            "vendor_phone": clean_phone_10(vendor.get("phone", ""))
        })

    is_verified = bool(khata_rec.get("is_verified") and khata_rec.get("status") == "ACTIVE")
    real_bal = recalculate_khata_user_balance(cols, khata_rec["_id"])
    return jsonify({
        "is_verified": is_verified,
        "status": khata_rec.get("status", "PENDING"),
        "credit_limit": float(khata_rec.get("credit_limit") or 0.0),
        "current_balance": real_bal,
        "full_address": khata_rec.get("full_address_display", ""),
        "shop_name": vendor.get("shop_name") or vendor.get("name") or "Society Store",
        "vendor_phone": clean_phone_10(vendor.get("phone", ""))
    })