import os, uuid, traceback, time, asyncio, base64, aiohttp, threading, re
from datetime import datetime, timedelta
from core.database import get_db

# --- CONFIGURATION VARIABLES ---
SUMMARY_LIMIT = 3           # Strictly only 3 AI summaries allowed
SUMMARY_COOLDOWN = 60       # Manual button retry cooldown
POST_COOLDOWN = 120         # Wait 120s after posting for auto-AI to finish
# -------------------------------

try:
    from google import genai
    from google.genai import types
    AI_LIBS_AVAILABLE = True
except ImportError:
    AI_LIBS_AVAILABLE = False

UPLOAD_FOLDER = '/home/ubuntu/web/webapp/static/announcement'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'txt'}

_key_index = 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ======================================================
# AI & METADATA LOGIC
# ======================================================

def _clean_ai_text(text):
    """
    Program-level cleaning: 
    1. Replaces more than 2 spaces with exactly 2 spaces.
    2. Removes markdown formatting characters.
    """
    if not text: return ""
    # Regex: Find 3 or more consecutive spaces and replace with 2 spaces
    text = re.sub(r' {3,}', '  ', text)
    # Strip markdown symbols
    text = re.sub(r'[*_~#>`]', '', text)
    return text.strip()

def _enforce_summary_and_eligibility(db_name):
    """Keeps summaries for Top 3 only. Permanently disqualifies 4th+ notices."""
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM notices ORDER BY created_at DESC LIMIT %s", (SUMMARY_LIMIT,))
        top_ids = [str(r['id']) for r in cursor.fetchall()]
        if top_ids:
            ids_str = ",".join(top_ids)
            cursor.execute(f"DELETE FROM notice_summaries WHERE notice_id NOT IN ({ids_str})")
            cursor.execute(f"UPDATE notice_metadata SET is_eligible = 0 WHERE notice_id NOT IN ({ids_str})")
        conn.commit()
        conn.close()
    except: pass

async def _ai_logic_wrapper(notice_id, title, content, file_path):
    db_name = os.getenv("DBs_NAME")
    
    # REFINED PROMPT: Focus on Data Density without listing individual names/IDs
    instr = (
        f"Create a short fact-sheet for the notice: '{title}'.\n"
        "RULES:\n"
        "1. OVERVIEW: State what the notice is about, the Date, and the Venue using simple words.\n"
        "2. DATA GROUPING: Do not list individual names, IDs, or application numbers. "
        "Instead, provide counts (e.g., '71 students listed') and list all unique branches, categories (SC/ST), and amounts.\n"
        "3. CONTACTS: Include all phone numbers and names of officials mentioned but don't mention more than 2.\n"
        "4. FORMAT: Use short, plain-text sentences. Strictly NO bold (**), NO italic (_), NO hashes (#).\n"
        "5. OMISSION: If a specific detail like Date is not in the text or files, skip it."
    )
    prompt = f"{instr}\n\nNotice Content: {content}"
    
    img_bytes, mime = None, "image/jpeg"
    if file_path:
        full_path = f"/home/ubuntu/web/webapp{file_path}"
        if os.path.exists(full_path):
            try:
                with open(full_path, 'rb') as f: img_bytes = f.read()
                if file_path.lower().endswith('.pdf'): 
                    mime = "application/pdf"
                elif file_path.lower().endswith('.png'):
                    mime = "image/png"
            except: pass

    keys = [k.strip() for k in os.getenv("GEMINI_KEYS", "").split(",") if k.strip()]
    global _key_index

    # --- PHASE 1: GEMINI ---
    for _ in range(len(keys)):
        current_key = keys[_key_index % len(keys)]
        _key_index += 1
        try:
            client = genai.Client(api_key=current_key)
            parts = [types.Part.from_text(text=prompt)]
            if img_bytes:
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
            
            res = await client.aio.models.generate_content(
                model="gemini-flash-latest", 
                contents=[types.Content(role="user", parts=parts)]
            )
            if res.text:
                _save_summary(notice_id, _clean_ai_text(res.text), db_name)
                return True, "Success"
        except: continue

    # --- PHASE 2: CLOUDFLARE ---
    cf_token, cf_acc, cf_model = os.getenv("CF_API_TOKEN"), os.getenv("CF_ACCOUNT_ID"), os.getenv("NCF_MODEL")
    if cf_token and cf_acc:
        try:
            url = f"https://api.cloudflare.com/client/v4/accounts/{cf_acc}/ai/run/{cf_model}"
            headers = {"Authorization": f"Bearer {cf_token}"}
            cf_msg = {"role": "user", "content": [{"type": "text", "text": prompt}]}
            if img_bytes and "image" in mime:
                b64_img = base64.b64encode(img_bytes).decode()
                cf_msg["content"].append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_img}"}})
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json={"messages": [cf_msg]}, timeout=25) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sum_text = data.get("result", {}).get("response")
                        if sum_text:
                            _save_summary(notice_id, _clean_ai_text(sum_text), db_name)
                            return True, "Success"
        except: pass
    return False, "Failed"

def _save_summary(notice_id, text, db_name):
    try:
        conn = get_db(db_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO notice_summaries (notice_id, summary) VALUES (%s, %s) ON DUPLICATE KEY UPDATE summary = %s", (notice_id, text, text))
        conn.commit()
        conn.close()
        _enforce_summary_and_eligibility(db_name)
    except: pass

def _start_background_ai(notice_id, title, content, file_path):
    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_ai_logic_wrapper(notice_id, title, content, file_path))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
    threading.Thread(target=run_loop, daemon=True).start()

# ======================================================
# HANDLERS
# ======================================================

def get_home_notices(db_name):
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT n.*, s.summary as ai_summary, m.is_eligible 
            FROM notices n 
            LEFT JOIN notice_summaries s ON n.id = s.notice_id 
            LEFT JOIN notice_metadata m ON n.id = m.notice_id
            ORDER BY n.created_at DESC
        """)
        notices = cursor.fetchall()
        conn.close()
        
        now = datetime.utcnow()
        for idx, n in enumerate(notices):
            # eligible: must be in top N AND was originally created inside top N
            # (is_eligible=1 at creation, demoted to 0 when pushed out; NULL treated as eligible)
            eligible = (idx < SUMMARY_LIMIT) and (n.get('is_eligible') != 0)
            seconds_since_post = (now - n['created_at']).total_seconds()
            within_30_days = n['created_at'] > (now - timedelta(days=30))
            n['has_summary'] = bool(n['ai_summary'])
            # show "AI Processing…" spinner — eligible post within the cooldown window
            n['is_locked'] = eligible and not n['has_summary'] and (seconds_since_post < POST_COOLDOWN)
            # show manual "AI Summary" generate button — eligible, past cooldown, within 30 days
            n['can_summarize'] = eligible and not n['has_summary'] and (seconds_since_post >= POST_COOLDOWN) and within_30_days
        return notices
    except: return []

def handle_post_notice(db_name, user, form, files):
    title, content = form.get('title'), form.get('content')
    file = files.get('attachment')
    creator = user.get('display_name') or user.get('name') or "Admin"
    file_url, file_type = None, None
    if file and file.filename != '' and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        new_fn = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, new_fn))
        file_url, file_type = f"/static/announcement/{new_fn}", ext
    try:
        conn = get_db(db_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO notices (title, content, file_path, file_type, created_by) VALUES (%s,%s,%s,%s,%s)", (title, content, file_url, file_type, creator))
        nid = cursor.lastrowid
        cursor.execute("INSERT INTO notice_metadata (notice_id, is_eligible) VALUES (%s, 1)", (nid,))
        conn.commit()
        conn.close()
        _enforce_summary_and_eligibility(db_name)
        _start_background_ai(nid, title, content, file_url)
        return True
    except: return False

def handle_edit_notice(db_name, id, form, files):
    title, content = form.get('title'), form.get('content')
    new_file = files.get('attachment')
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT file_path FROM notices WHERE id = %s", (id,))
        row = cursor.fetchone()
        path = row['file_path'] if row else None
        if new_file and new_file.filename != '' and allowed_file(new_file.filename):
            if path and os.path.exists(f"/home/ubuntu/web/webapp{path}"): os.remove(f"/home/ubuntu/web/webapp{path}")
            ext = new_file.filename.rsplit('.', 1)[1].lower()
            new_fn = f"{uuid.uuid4().hex}.{ext}"
            new_file.save(os.path.join(UPLOAD_FOLDER, new_fn))
            path = f"/static/announcement/{new_fn}"
        cursor.execute("UPDATE notices SET title=%s, content=%s, file_path=%s WHERE id=%s", (title, content, path, id))
        conn.commit()
        conn.close()
        _start_background_ai(id, title, content, path)
        return True
    except: return False

def handle_delete_notice(db_name, id):
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT file_path FROM notices WHERE id = %s", (id,))
        row = cursor.fetchone()
        if row and row['file_path']:
            p = f"/home/ubuntu/web/webapp{row['file_path']}"
            if os.path.exists(p): os.remove(p)
        cursor.execute("DELETE FROM notices WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        _enforce_summary_and_eligibility(db_name)
        return True
    except: return False

def handle_manual_summary(db_name, notice_id):
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT n.title, n.content, n.file_path, n.created_at, m.is_eligible, m.last_ai_request 
            FROM notices n LEFT JOIN notice_metadata m ON n.id = m.notice_id WHERE n.id = %s
        """, (notice_id,))
        n = cursor.fetchone()
        if not n or n.get('is_eligible') == 0:
            conn.close()
            return {"status": "error", "msg": "Notice not eligible."}
        now = datetime.utcnow()
        if (now - n['created_at']).total_seconds() < POST_COOLDOWN:
            conn.close()
            return {"status": "error", "msg": "Still processing post."}
        if n['last_ai_request'] and (now - n['last_ai_request']).total_seconds() < SUMMARY_COOLDOWN:
            conn.close()
            return {"status": "error", "msg": "Cooldown active."}
        cursor.execute("UPDATE notice_metadata SET last_ai_request = %s WHERE notice_id = %s", (now, notice_id))
        conn.commit()
        conn.close()
        success, reason = asyncio.run(_ai_logic_wrapper(notice_id, n['title'], n['content'], n['file_path']))
        return {"status": "success"} if success else {"status": "error", "msg": "AI Failed", "reason": reason}
    except Exception as e:
        return {"status": "error", "msg": str(e)}