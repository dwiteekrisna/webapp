import os, re, json, io, asyncio, base64, aiohttp, time
from datetime import datetime
from core.database import get_db

# DYNAMIC IMPORTS
try:
    from google import genai
    from google.genai import types
    from PIL import Image
    LIB_AVAILABLE = True
except ImportError:
    LIB_AVAILABLE = False

class LacebitAIEngine:
    # --- TOGGLE CONSOLE LOGGING ---
    DEBUG_MODE = False 

    def __init__(self):
        # 1. Gemini Config
        self.api_keys = [k.strip() for k in os.getenv("GEMINI_KEYS", "").split(",") if k.strip()]
        self.model_name = "gemini-flash-lite-latest"
        self.current_key_idx = 0
        self.clients = {} 
        self.blocked_keys = {} # {key: expiry_timestamp}
        self.lock = asyncio.Lock()
        
        # 2. Cloudflare Config
        self.cf_account_id = os.getenv("CF_ACCOUNT_ID")
        self.cf_token = os.getenv("CF_API_TOKEN")
        self.cf_model = os.getenv("CF_MODEL", "@cf/meta/llama-3.2-11b-vision-instruct")

        # 3. Database
        self.db_name = os.getenv("DB_NAME")

        # FIX: bot_info_cache was declared but never used — now actively used to skip
        #      repeated DB hits for data that almost never changes.
        self.bot_info_cache = None          # Cached bot identity row
        self.bot_cache_expiry = 0           # Unix timestamp; refreshed every 10 min

        if self.DEBUG_MODE:
            self._log(f"LacebitAIEngine Initialized. Keys: {len(self.api_keys)}")

    # ======================================================
    # HELPER UTILITIES
    # ======================================================

    def _log(self, message):
        """Unified logging that strictly respects DEBUG_MODE."""
        if self.DEBUG_MODE:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _get_gemini_client(self, api_key):
        if api_key not in self.clients:
            self.clients[api_key] = genai.Client(api_key=api_key)
        return self.clients[api_key]

    async def _get_next_available_key(self):
        """O(1) Round Robin key selector with Blacklist check."""
        now = time.time()
        async with self.lock:
            # Cleanup expired blocks
            self.blocked_keys = {k: exp for k, exp in self.blocked_keys.items() if exp > now}
            
            for _ in range(len(self.api_keys)):
                idx = self.current_key_idx
                key = self.api_keys[idx]
                self.current_key_idx = (idx + 1) % len(self.api_keys)

                if key not in self.blocked_keys:
                    return key, idx + 1, self._get_gemini_client(key)
                
                self._log(f"SKIPPING Key {idx+1} (Blacklisted)")
            return None, None, None

    # ======================================================
    # DATABASE SEARCH LOGIC (PARALLEL)
    # ======================================================

    def _db_fetch_bot_identity(self):
        """
        OPTIMIZATION: Returns cached bot identity if still fresh (TTL = 10 min).
        Eliminates a DB round-trip on every message for data that is effectively static.
        Cache is instance-level so it is shared across all concurrent requests.
        Falls back to stale cache on DB error rather than returning None.
        """
        now = time.time()
        if self.bot_info_cache and now < self.bot_cache_expiry:
            self._log("DB Identity: cache HIT")
            return self.bot_info_cache

        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT bot_name, developer, habits FROM bot_info WHERE id = 1")
            res = cursor.fetchone()
            conn.close()
            # Populate cache only on a valid result
            if res:
                self.bot_info_cache = res
                self.bot_cache_expiry = now + 600   # 10-minute TTL
            self._log("DB Identity: cache MISS — refreshed")
            return res
        except Exception as e:
            self._log(f"DB Identity Error: {e}")
            return self.bot_info_cache   # Serve stale cache on error rather than None

    def _db_fetch_principal(self, msg_l):
        if "principal" not in msg_l: return None
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, phone FROM principal LIMIT 1")
            res = cursor.fetchone()
            conn.close()
            return f"Principal: {res['name']} ({res['phone']})" if res else None
        except: return None

    def _db_fetch_hod(self, msg_l):
        if "hod" not in msg_l: return None
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, branch, phone FROM faculty WHERE designation LIKE %s", ("%HOD%",))
            rows = cursor.fetchall()
            conn.close()
            return "\n".join([f"HOD: {r['name']} ({r['branch']}) {r['phone']}" for r in rows])
        except: return None

    def _db_fetch_fuzzy_staff(self, msg_l):
        dept_map = {
            'CSE': ['cse', 'computer', 'cse', 'comp'], 'WORKSHOP': ['workshop', 'w/s'],
            'TEXTILE': ['textile', 'tex'], 'ELECTRICAL': ['electrical', 'ece', 'elect'],
            'MECHANICAL': ['mechanical', 'mech'], 'Math & Sc.': ['math', 'science', 'physics', 'chemistry']
        }
        target_branches = [br for br, kws in dept_map.items() if any(kw in msg_l for kw in kws)]
        clean = re.sub(r'who is|tell|contact|find|faculty|staff|list|department|hod|principal|sir|maam', '', msg_l).strip()
        if not target_branches and len(clean) < 3: return None
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            search_terms, params = [], []
            for br in target_branches: search_terms.append("branch = %s"); params.append(br)
            if len(clean) > 2: search_terms.append("name LIKE %s"); params.append(f"%{clean}%")
            where = " OR ".join(search_terms)
            results = []
            cursor.execute(f"SELECT name, branch, phone FROM faculty WHERE {where} LIMIT 30", params)
            for r in cursor.fetchall(): results.append(f"Faculty: {r['name']} ({r['branch']}) {r['phone']}")
            cursor.execute(f"SELECT name, branch, phone FROM non_teaching_staff WHERE {where} LIMIT 20", params)
            for r in cursor.fetchall(): results.append(f"Staff: {r['name']} ({r['branch']}) {r['phone']}")
            conn.close()
            return "\n".join(results)
        except: return None

    def _optimize_image_sync(self, image_bytes):
        if not LIB_AVAILABLE or not image_bytes: return image_bytes
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((800, 800))
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=70)
            return out.getvalue()
        except: return image_bytes

    # ======================================================
    # CORE ENGINE LOGIC
    # ======================================================

    async def generate_response_stream(self, user_message, history=None, image_bytes=None, mime_type=None, user_name="Student"):
        # 1. PARALLEL DATA GATHERING
        msg_l = user_message.lower().strip()
        db_tasks = [
            asyncio.to_thread(self._db_fetch_bot_identity),
            asyncio.to_thread(self._db_fetch_principal, msg_l),
            asyncio.to_thread(self._db_fetch_hod, msg_l),
            asyncio.to_thread(self._db_fetch_fuzzy_staff, msg_l)
        ]
        if image_bytes:
            db_tasks.append(asyncio.to_thread(self._optimize_image_sync, image_bytes))
        
        results = await asyncio.gather(*db_tasks)
        bot, principal, hod, fuzzy = results[0], results[1], results[2], results[3]
        opt_image = results[4] if image_bytes else None

        # FIX: Bot now receives the user's FULL name for identity awareness,
        #      but continues to address them by first name only for a friendly tone.
        u_full  = user_name if user_name else "Student"
        u_first = u_full.split()[0]

        # 2. TOKEN-EFFICIENT SYSTEM INSTRUCTION
        db_data = "\n".join(filter(None, [principal, hod, fuzzy]))
        sys_instr = (
            f"You are {bot['bot_name'] if bot else 'AI'} by {bot['developer'] if bot else 'Dev'}. Style: {bot['habits'] if bot else 'concise'}. "
            f"User full name: {u_full}. Address them as: {u_first}. DATA: {db_data}. "
            f"RULES: 1. Address as {u_first}. 2. You != {u_first}. "
            f"3. Format Names in **Bold**. 4. Use _Italic_, ***Bold+Italic***, ###, >, `Code`. 5. Moderated length."
        )

        # 3. HISTORY DEDUPLICATION
        formatted_history = []
        if history:
            for e in history[-4:]:
                role, txt = ("model" if e.get('role') == "assistant" else "user"), e.get('content', '').strip()
                if role == "user" and txt == user_message.strip(): continue
                if txt: formatted_history.append(types.Content(role=role, parts=[types.Part.from_text(text=txt)]))

        failure_reason, should_fallback = "other", False

        # ─── STEP 1: GEMINI ───
        for i in range(len(self.api_keys)):
            key, key_num, client = await self._get_next_available_key()
            if not key: should_fallback = True; break
            
            self._log(f"[USE] Gemini Key {key_num}")
            try:
                contents = formatted_history.copy()
                new_parts = []
                if opt_image: new_parts.append(types.Part.from_bytes(data=opt_image, mime_type="image/jpeg"))
                new_parts.append(types.Part.from_text(text=user_message))
                contents.append(types.Content(role="user", parts=new_parts))

                safety = [types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]]

                yielded = False
                async for chunk in await client.aio.models.generate_content_stream(
                    model=self.model_name, contents=contents, 
                    config=types.GenerateContentConfig(system_instruction=sys_instr, temperature=0.7, safety_settings=safety)
                ):
                    if chunk.text: yield str(chunk.text); yielded = True
                
                if yielded: 
                    self._log(f"[SUCCESS] Gemini Key {key_num}")
                    return 
                else: raise Exception("SAFETY_BLOCK")

            except Exception as e:
                err = str(e)
                if "SAFETY" in err or "finish_reason: 3" in err:
                    self._log(f"[BLOCK] Key {key_num} safety triggered. Switching to Cloudflare.")
                    should_fallback = True
                    break 
                else:
                    async with self.lock: self.blocked_keys[key] = time.time() + 600
                    self._log(f"[FAIL] Key {key_num}: {err}")
                    if "429" in err: failure_reason = "429"
                    if i == len(self.api_keys) - 1: should_fallback = True
                    continue 

        # ─── STEP 2: CLOUDFLARE FALLBACK ───
        if should_fallback and self.cf_token:
            self._log(f"[FALLBACK] Switching to Cloudflare ({self.cf_model})...")
            url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/run/{self.cf_model}"
            headers = {"Authorization": f"Bearer {self.cf_token}", "X-Skip-Model-Agreement": "true"}
            
            cf_msgs = [{"role": "system", "content": sys_instr}]
            for e in history[-4:] if history else []:
                r, t = ("assistant" if e.get('role') == 'assistant' else 'user'), e.get('content','').strip()
                if r == 'user' and t == user_message.strip(): continue
                cf_msgs.append({"role": r, "content": t})
            cf_msgs.append({"role": "user", "content": user_message})

            payload = {"messages": cf_msgs, "stream": True, "max_tokens": 2048}
            if opt_image: payload["image"] = list(opt_image)

            async with aiohttp.ClientSession() as session:
                for _ in range(2):
                    try:
                        async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                            if resp.status == 200:
                                buffer = ""
                                async for chunk_bytes in resp.content.iter_any():
                                    buffer += chunk_bytes.decode('utf-8', errors='ignore')
                                    while "\n" in buffer:
                                        line, buffer = buffer.split("\n", 1)
                                        if line.startswith("data:"):
                                            # FIX: renamed from `data` → `cf_payload` to prevent
                                            #      shadowing the outer `db_data` variable
                                            cf_payload = line[5:].strip()
                                            if cf_payload == "[DONE]": break
                                            try:
                                                token = json.loads(cf_payload).get("response")
                                                if token: yield str(token)
                                            except: continue
                                self._log("[SUCCESS] Cloudflare stream completed.")
                                return
                            elif resp.status == 403:
                                self._log("[LICENSE] Sending agree handshake...")
                                await session.post(url, headers=headers, json={"prompt": "agree"})
                                continue 
                            break
                    except Exception as e:
                        self._log(f"[CF ERROR] {e}")
                        break

        # ─── STEP 3: FINAL ERROR HANDLING ───
        if failure_reason == "429":
            yield "we are facing high traffic kindly wait !! 🙏 We are facing token shortage issue"
        else:
            yield "we are facing high traffic kindly retry after sometimes"

# Instantiate
ai_engine = LacebitAIEngine()

async def get_ai_response_stream(user_msg, history=None, img_bytes=None, mime_type=None, user_name="Student"):
    async for chunk in ai_engine.generate_response_stream(user_msg, history, img_bytes, mime_type, user_name):
        yield chunk
