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
    DEBUG_MODE = True  # Set False in production

    def __init__(self):
        # 1. Gemini Config
        self.api_keys =[k.strip() for k in os.getenv("GEMINI_KEYS", "").split(",") if k.strip()]
        self.model_name = "gemini-flash-lite-latest"
        self.current_key_idx = 0
        self.clients = {} 
        self.blocked_keys = {} # {key: expiry_timestamp}
        self.lock = asyncio.Lock()
        
        # 2. Cloudflare Config (Updated to Default user preference)
        self.cf_account_id = os.getenv("CF_ACCOUNT_ID")
        self.cf_token = os.getenv("CF_API_TOKEN")
        self.cf_model = os.getenv("CF_MODEL", "@cf/moonshotai/kimi-k2.5")

        # 3. Database
        self.db_name = os.getenv("DB_NAME")

        # OPTIMIZATION: Heavy Memory Caching for Cold-Start Database Lookups.
        # This prevents the App from crashing/hanging when doing parallel DB queries on new loads.
        self.db_cache = {
            'bot_info': {'data': None, 'expiry': 0},
            'principal': {'data': None, 'expiry': 0},
            'hod': {'data': None, 'expiry': 0},
            'fuzzy': {} # {search_term: {'data': string, 'expiry': timestamp}}
        }

        if self.DEBUG_MODE:
            self._log(f"LacebitAIEngine Initialized. Keys: {len(self.api_keys)}")

    # ======================================================
    # HELPER UTILITIES
    # ======================================================

    def _log(self, message):
        if self.DEBUG_MODE:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)

    def _get_gemini_client(self, api_key):
        if api_key not in self.clients:
            self.clients[api_key] = genai.Client(api_key=api_key)
        return self.clients[api_key]

    async def _get_next_available_key(self):
        now = time.time()
        async with self.lock:
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
    # DATABASE SEARCH LOGIC (WITH CACHE TO FIX COLD STARTS)
    # ======================================================

    def _db_fetch_bot_identity(self):
        now = time.time()
        if now < self.db_cache['bot_info']['expiry']:
            return self.db_cache['bot_info']['data']

        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT bot_name, developer, habits FROM bot_info WHERE id = 1")
            res = cursor.fetchone()
            conn.close()
            # Cache for 1 Hour to save Connection time overhead
            self.db_cache['bot_info'] = {'data': res, 'expiry': now + 3600}
            return res
        except Exception as e:
            self._log(f"DB Identity Error: {e}")
            return self.db_cache['bot_info']['data']

    def _db_fetch_principal(self, msg_l):
        if "principal" not in msg_l: return None
        now = time.time()
        if now < self.db_cache['principal']['expiry']:
            return self.db_cache['principal']['data']
            
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, phone FROM principal LIMIT 1")
            res = cursor.fetchone()
            conn.close()
            data = f"Principal: {res['name']} ({res['phone']})" if res else None
            self.db_cache['principal'] = {'data': data, 'expiry': now + 3600}
            return data
        except: return None

    def _db_fetch_hod(self, msg_l):
        if "hod" not in msg_l: return None
        now = time.time()
        if now < self.db_cache['hod']['expiry']:
            return self.db_cache['hod']['data']
            
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT name, branch, phone FROM faculty WHERE designation LIKE %s", ("%HOD%",))
            rows = cursor.fetchall()
            conn.close()
            data = "\n".join([f"HOD: {r['name']} ({r['branch']}) {r['phone']}" for r in rows])
            self.db_cache['hod'] = {'data': data, 'expiry': now + 3600}
            return data
        except: return None

    def _db_fetch_fuzzy_staff(self, msg_l):
        dept_map = {
            'CSE': ['cse', 'computer', 'cse', 'comp'], 'WORKSHOP':['workshop', 'w/s'],
            'TEXTILE': ['textile', 'tex'], 'ELECTRICAL':['electrical', 'ece', 'elect'],
            'MECHANICAL': ['mechanical', 'mech'], 'Math & Sc.': ['math', 'science', 'physics', 'chemistry']
        }
        target_branches =[br for br, kws in dept_map.items() if any(kw in msg_l for kw in kws)]
        clean = re.sub(r'who is|tell|contact|find|faculty|staff|list|department|hod|principal|sir|maam', '', msg_l).strip()
        
        if not target_branches and len(clean) < 3: return None
        
        cache_key = f"{','.join(target_branches)}_{clean}"
        now = time.time()
        if cache_key in self.db_cache['fuzzy'] and now < self.db_cache['fuzzy'][cache_key]['expiry']:
            return self.db_cache['fuzzy'][cache_key]['data']
            
        try:
            conn = get_db(self.db_name)
            cursor = conn.cursor(dictionary=True)
            search_terms, params = [],[]
            for br in target_branches: search_terms.append("branch = %s"); params.append(br)
            if len(clean) > 2: search_terms.append("name LIKE %s"); params.append(f"%{clean}%")
            where = " OR ".join(search_terms)
            results =[]
            cursor.execute(f"SELECT name, branch, phone FROM faculty WHERE {where} LIMIT 30", params)
            for r in cursor.fetchall(): results.append(f"Faculty: {r['name']} ({r['branch']}) {r['phone']}")
            cursor.execute(f"SELECT name, branch, phone FROM non_teaching_staff WHERE {where} LIMIT 20", params)
            for r in cursor.fetchall(): results.append(f"Staff: {r['name']} ({r['branch']}) {r['phone']}")
            conn.close()
            
            data = "\n".join(results)
            self.db_cache['fuzzy'][cache_key] = {'data': data, 'expiry': now + 3600}
            return data
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
        # 1. PARALLEL DATA GATHERING (Now Cache-Optimized!)
        msg_l = user_message.lower().strip()
        db_tasks =[
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

        u_full  = user_name if user_name else "Student"
        u_first = u_full.split()[0]

        # 2. TOKEN-EFFICIENT SYSTEM INSTRUCTION
        db_data = "\n".join(filter(None, [principal, hod, fuzzy]))
        sys_instr = (
            f"You are {bot['bot_name'] if bot else 'AI'} by {bot['developer'] if bot else 'Dev'}. Style: {bot['habits'] if bot else 'concise'}. "
            f"User full name: {u_full}. Address them as: {u_first}. DATA: {db_data}. "
            f"RULES: 1. Address as {u_first}. 2. You != {u_first}. "
            f"3. Format Names in **Bold**. 4. Use _Italic_, ***Bold+Italic***, ###, >, `Code`."
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
                new_parts =[]
                if opt_image: new_parts.append(types.Part.from_bytes(data=opt_image, mime_type="image/jpeg"))
                new_parts.append(types.Part.from_text(text=user_message))
                contents.append(types.Content(role="user", parts=new_parts))

                safety =[types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]]

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
            self._log(f"[FALLBACK] Switching to Cloudflare model: {self.cf_model}")
            t_fallback_start = time.time()

            url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/run/{self.cf_model}"
            headers = {"Authorization": f"Bearer {self.cf_token}", "X-Skip-Model-Agreement": "true"}

            cf_msgs =[{"role": "system", "content": sys_instr}]
            for e in history[-4:] if history else[]:
                r, t = ("assistant" if e.get('role') == 'assistant' else 'user'), e.get('content', '').strip()
                if r == 'user' and t == user_message.strip(): continue
                if t: cf_msgs.append({"role": r, "content": t})

            if opt_image:
                b64_img = base64.b64encode(opt_image).decode("utf-8")
                user_content =[
                    {"type": "text", "text": user_message},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                ]
            else:
                user_content = user_message

            cf_msgs.append({"role": "user", "content": user_content})
            payload = {"messages": cf_msgs, "stream": True, "max_tokens": 2048}

            # Extended connection wait duration for cold starts, keeping stream active
            timeout = aiohttp.ClientTimeout(connect=25, sock_read=120)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(2):
                    try:
                        self._log(f"[CF] Attempt {attempt + 1} — POST {url}")
                        async with session.post(url, headers=headers, json=payload) as resp:
                            self._log(f"[CF] HTTP status: {resp.status}")

                            if resp.status == 200:
                                token_count = 0
                                # OPTIMIZATION: Safely parsing byte-chunks prevents `utf-8` split character 
                                # drops which normally cause silent parsing failures and lag.
                                buffer = b""
                                
                                async for raw_chunk in resp.content.iter_any():
                                    buffer += raw_chunk
                                    
                                    while b'\n' in buffer:
                                        line_bytes, buffer = buffer.split(b'\n', 1)
                                        # Parse Line Fast and skip broken encodings mid-stream
                                        line = line_bytes.decode('utf-8', errors='ignore').strip()
                                        
                                        if not line.startswith("data:"): continue
                                        cf_payload = line[5:].strip()
                                        
                                        if cf_payload == "[DONE]":
                                            self._log(f"[CF] Stream done. Tokens: {token_count}, Time: {time.time() - t_fallback_start:.2f}s")
                                            return
                                        try:
                                            parsed = json.loads(cf_payload)
                                            # Compatible with Standard Moonshot / Llama Responses
                                            token = ""
                                            if "choices" in parsed and len(parsed["choices"]) > 0:
                                                delta = parsed["choices"][0].get("delta", {})
                                                token = delta.get("content", "")
                                            elif "response" in parsed:
                                                token = parsed["response"]
                                                
                                            if token:
                                                token_count += 1
                                                yield str(token)
                                        except json.JSONDecodeError:
                                            continue

                                self._log(f"[CF] Stream exhausted. Tokens: {token_count}, Time: {time.time() - t_fallback_start:.2f}s")
                                return

                            elif resp.status == 403:
                                self._log("[CF] 403 — Sending license agree handshake...")
                                await session.post(url, headers=headers, json={"prompt": "agree"})
                                continue
                            else:
                                body = await resp.text()
                                self._log(f"[CF ERROR] HTTP {resp.status} — {body[:200]}")
                            break

                    except asyncio.TimeoutError:
                        self._log(f"[CF ERROR] Timeout after {time.time() - t_fallback_start:.2f}s")
                        break
                    except Exception as e:
                        self._log(f"[CF ERROR] {type(e).__name__}: {e}")
                        break

        # ─── STEP 3: FINAL ERROR HANDLING ───
        if failure_reason == "429":
            yield "we are facing high traffic kindly wait !! 🙏 We are facing token shortage issue"
        else:
            yield "we are facing high traffic kindly retry after sometimes"

# Instantiate
ai_engine = LacebitAIEngine()

async def get_ai_response_stream(user_msg, history=None, img_bytes=None, mime_type=None, user_name="Student"):
    gen = ai_engine.generate_response_stream(user_msg, history, img_bytes, mime_type, user_name)
    try:
        async for chunk in gen:
            yield chunk
    except GeneratorExit:
        pass
    finally:
        await gen.aclose()