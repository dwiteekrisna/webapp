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
        # Gemini Config
        self.api_keys = [k.strip() for k in os.getenv("GEMINI_KEYS", "").split(",") if k.strip()]
        self.model_name = "gemini-flash-lite-latest"
        self.current_key_idx = 0
        self.clients = {} 

        # IN-MEMORY BLACKLIST
        self.blocked_keys = {} 

        # Cloudflare Config
        self.cf_account_id = os.getenv("CF_ACCOUNT_ID")
        self.cf_token = os.getenv("CF_API_TOKEN")
        self.cf_model = os.getenv("CF_MODEL", "@cf/meta/llama-3.2-11b-vision-instruct")

        self.db_name = os.getenv("DB_NAME")
        self.lock = asyncio.Lock()
        
        if self.DEBUG_MODE:
            print(f"[INIT] LacebitAIEngine: {len(self.api_keys)} Gemini keys detected.")

    def _log(self, message):
        if self.DEBUG_MODE: print(message)

    def _get_client(self, api_key):
        if api_key not in self.clients:
            self.clients[api_key] = genai.Client(api_key=api_key)
        return self.clients[api_key]

    async def _get_next_gemini(self):
        """Ultra-fast O(1) Round Robin + Blacklist check."""
        now = time.time()
        async with self.lock:
            for _ in range(len(self.api_keys)):
                idx = self.current_key_idx
                key = self.api_keys[idx]
                self.current_key_idx = (idx + 1) % len(self.api_keys)
                blocked_until = self.blocked_keys.get(key, 0)
                if blocked_until <= now:
                    if blocked_until > 0: del self.blocked_keys[key] 
                    self._log(f"[USE] Gemini Key {idx + 1}")
                    return key, idx + 1, self._get_client(key)
                
                res_t = datetime.fromtimestamp(blocked_until).strftime('%H:%M:%S')
                self._log(f"[SKIP] Key {idx + 1} blocked until {res_t}")
            return None, None, None

    def _sync_get_context(self, user_msg, user_display_name):
        """Search logic including BOT IDENTITY, HABITS, and personalized names."""
        msg_l = user_msg.lower().strip()
        context = []
        conn = get_db(self.db_name)
        if not conn: return ""
        try:
            cursor = conn.cursor(dictionary=True)
            
            # 1. Identity & Habits (RESTORED)
            cursor.execute("SELECT bot_name, developer, habits FROM bot_info WHERE id = 1")
            bot = cursor.fetchone()
            if bot:
                context.append(f"AI Identity: You are {bot['bot_name']} by {bot['developer']}. Your personality/habits: {bot['habits']}. You are talking to a human named: {user_display_name} , call them with first name.")
                
                # Fast track for greetings/name checks
                greetings_and_name_queries = {'hi', 'hello', 'hey', 'nexus', 'ai', "what's my name", "who am i"}
                if any(q in msg_l for q in greetings_and_name_queries):
                    conn.close()
                    return f"Instruction: Greet {user_display_name} using your habits: {bot['habits']}. Identity: {bot['bot_name']} by {bot['developer']}."

            # 2. Campus Logic (Principal, HOD, Faculty)
            if "principal" in msg_l:
                cursor.execute("SELECT name, phone FROM principal LIMIT 1")
                p = cursor.fetchone()
                if p: context.append(f"Principal: {p['name']} (Ph: {p['phone']})")

            if "hod" in msg_l:
                cursor.execute("SELECT name, designation, branch, phone FROM faculty WHERE designation LIKE %s", ("%HOD%",))
                for r in cursor.fetchall():
                    context.append(f"HOD: {r['name']} ({r['designation']}) Dept: {r['branch']} Ph: {r['phone']}")

            clean = re.sub(r'who is|tell|contact|find|faculty|staff|list|department|hod|principal|sir|maam', '', msg_l).strip()
            if len(clean) > 2:
                cursor.execute("SELECT name, designation, branch, phone FROM faculty WHERE name LIKE %s LIMIT 30", (f"%{clean}%",))
                for r in cursor.fetchall(): context.append(f"Faculty: {r['name']} | {r['branch']} | Ph: {r['phone']}")
            
            conn.close()
        except: pass
        return "\n".join(list(set(context)))

    def _sync_optimize_image(self, image_bytes, size=(800, 800)):
        if not LIB_AVAILABLE or not image_bytes: return image_bytes
        try:
            img = Image.open(io.BytesIO(image_bytes))
            if img.mode != "RGB": img = img.convert("RGB")
            img.thumbnail(size)
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=70, optimize=True)
            return out.getvalue()
        except: return image_bytes

    async def generate_response_stream(self, user_message, history=None, image_bytes=None, mime_type=None, user_name="Student"):
        # 1. PARALLEL START
        tasks = [asyncio.to_thread(self._sync_get_context, user_message, user_name)]
        if image_bytes: tasks.append(asyncio.to_thread(self._sync_optimize_image, image_bytes))
        results = await asyncio.gather(*tasks)
        db_context, opt_image = results[0], (results[1] if len(results) > 1 else None)

        # TOUGH System Instruction
        sys_instr = (
            f"SYSTEM: Chatting with {user_name}. Use this REAL NAME. Never call them with Full name mentioned with first name\n"
            f"CAMPUS DATA:\n{db_context}\n\n"
            f"INSTRUCTION: Use the HABITS and DATA provided. Bold **names**. Friendly/direct."
        )

        # 2. DEDUPLICATION
        formatted_history = []
        if history:
            for entry in history[-4:]:
                role = "model" if entry.get('role') == "assistant" else "user"
                txt = entry.get('content', '').strip()
                if role == "user" and txt == user_message.strip(): continue
                if txt: formatted_history.append(types.Content(role=role, parts=[types.Part.from_text(text=txt)]))

        failure_reason = "other"
        should_fallback = False

        # --- STEP 1: GEMINI ---
        for i in range(len(self.api_keys)):
            key, key_num, client = await self._get_next_gemini()
            if not key: should_fallback = True; break
            try:
                contents = formatted_history.copy()
                new_parts = []
                if opt_image: new_parts.append(types.Part.from_bytes(data=opt_image, mime_type="image/jpeg"))
                new_parts.append(types.Part.from_text(text=user_message))
                contents.append(types.Content(role="user", parts=new_parts))

                async for chunk in await client.aio.models.generate_content_stream(
                    model=self.model_name, contents=contents, 
                    config=types.GenerateContentConfig(system_instruction=sys_instr, temperature=0.7)
                ):
                    if chunk.text: yield str(chunk.text)
                return 

            except Exception as e:
                err = str(e)
                async with self.lock: self.blocked_keys[key] = time.time() + 600 
                self._log(f"[FAIL] Key {key_num}: {err}")
                if "429" in err:
                    failure_reason = "429"
                    if i == len(self.api_keys) - 1: should_fallback = True
                    continue 
                else:
                    failure_reason = "other"
                    should_fallback = True
                    break

        # --- STEP 2: CLOUDFLARE FALLBACK (Fixed Buffer-Aware Parser) ---
        if should_fallback and self.cf_token:
            url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/run/{self.cf_model}"
            headers = {"Authorization": f"Bearer {self.cf_token}", "X-Skip-Model-Agreement": "true"}
            
            cf_msgs = [{"role": "system", "content": sys_instr}]
            for e in history[-4:] if history else []:
                r, t = ("assistant" if e.get('role') == "assistant" else "user"), e.get('content','').strip()
                if r == "user" and t == user_message.strip(): continue
                cf_msgs.append({"role": r, "content": t})
            cf_msgs.append({"role": "user", "content": user_message})

            payload = {"messages": cf_msgs, "stream": True, "max_tokens": 2048}
            if opt_image:
                cf_img = await asyncio.to_thread(self._sync_optimize_image, image_bytes, (600, 600))
                payload["image"] = list(cf_img)

            async with aiohttp.ClientSession() as session:
                for cf_attempt in range(2):
                    try:
                        async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                            if resp.status == 200:
                                buffer = ""
                                async for chunk_bytes in resp.content.iter_any():
                                    buffer += chunk_bytes.decode('utf-8', errors='ignore')
                                    while "\n" in buffer:
                                        line, buffer = buffer.split("\n", 1)
                                        if line.startswith("data:"):
                                            data_raw = line[5:].strip()
                                            if data_raw == "[DONE]": break
                                            try:
                                                data_json = json.loads(data_raw)
                                                token = data_json.get("response")
                                                if token is not None: yield str(token)
                                            except: continue
                                return
                            elif resp.status == 403 and cf_attempt == 0:
                                await session.post(url, headers=headers, json={"prompt": "agree"})
                                continue 
                            break
                    except: break

        # --- STEP 3: FINAL ERRORS ---
        if failure_reason == "429":
            yield "we are facing high traffic kindly wait !! 🙏 We are facing token shortage issue"
        else:
            yield "we are facing high traffic kindly retry after sometimes"

# Instantiate engine
ai_engine = LacebitAIEngine()

async def get_ai_response_stream(user_msg, history=None, img_bytes=None, mime_type=None, user_name="Student"):
    async for chunk in ai_engine.generate_response_stream(user_msg, history, img_bytes, mime_type, user_name):
        yield chunk