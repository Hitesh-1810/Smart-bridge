from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, stream_with_context
from groq import Groq
from dotenv import load_dotenv
from fuzzywuzzy import process as fuzz_process
import os, time, hashlib, json, base64, re

load_dotenv()

app = Flask(__name__)
app.secret_key = "medsafe_secret_2024"
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL = "llama-3.3-70b-versatile"

# ── Medicine database with known interactions ─────────────────────────────────
MEDICINE_DB = {
    "warfarin":      {"salt": "Warfarin Sodium",    "category": "Anticoagulant"},
    "aspirin":       {"salt": "Acetylsalicylic Acid","category": "NSAID / Antiplatelet"},
    "ibuprofen":     {"salt": "Ibuprofen",           "category": "NSAID"},
    "paracetamol":   {"salt": "Acetaminophen",       "category": "Analgesic"},
    "metformin":     {"salt": "Metformin HCl",       "category": "Antidiabetic"},
    "atorvastatin":  {"salt": "Atorvastatin Calcium","category": "Statin"},
    "amoxicillin":   {"salt": "Amoxicillin Trihydrate","category":"Antibiotic"},
    "ciprofloxacin": {"salt": "Ciprofloxacin HCl",  "category": "Antibiotic"},
    "omeprazole":    {"salt": "Omeprazole",          "category": "PPI"},
    "lisinopril":    {"salt": "Lisinopril",          "category": "ACE Inhibitor"},
    "amlodipine":    {"salt": "Amlodipine Besylate", "category": "Calcium Channel Blocker"},
    "metoprolol":    {"salt": "Metoprolol Tartrate", "category": "Beta Blocker"},
    "clopidogrel":   {"salt": "Clopidogrel Bisulfate","category":"Antiplatelet"},
    "diazepam":      {"salt": "Diazepam",            "category": "Benzodiazepine"},
    "sertraline":    {"salt": "Sertraline HCl",      "category": "SSRI"},
    "fluoxetine":    {"salt": "Fluoxetine HCl",      "category": "SSRI"},
    "cetirizine":    {"salt": "Cetirizine HCl",      "category": "Antihistamine"},
    "azithromycin":  {"salt": "Azithromycin",        "category": "Antibiotic"},
    "doxycycline":   {"salt": "Doxycycline Hyclate", "category": "Antibiotic"},
    "prednisolone":  {"salt": "Prednisolone",        "category": "Corticosteroid"},
    "insulin":       {"salt": "Insulin (various)",   "category": "Antidiabetic"},
    "digoxin":       {"salt": "Digoxin",             "category": "Cardiac Glycoside"},
    "furosemide":    {"salt": "Furosemide",          "category": "Loop Diuretic"},
    "pantoprazole":  {"salt": "Pantoprazole Sodium", "category": "PPI"},
    "ranitidine":    {"salt": "Ranitidine HCl",      "category": "H2 Blocker"},
}

KNOWN_INTERACTIONS = [
    ("warfarin",    "aspirin",       "HIGH",   "Increased bleeding risk — both thin the blood."),
    ("warfarin",    "ibuprofen",     "HIGH",   "NSAIDs increase anticoagulant effect of warfarin."),
    ("warfarin",    "ciprofloxacin", "MODERATE","Ciprofloxacin may increase warfarin levels."),
    ("aspirin",     "ibuprofen",     "MODERATE","Ibuprofen may reduce aspirin's antiplatelet effect."),
    ("metformin",   "furosemide",    "MODERATE","Furosemide may increase metformin levels."),
    ("sertraline",  "fluoxetine",    "HIGH",   "Combining two SSRIs raises serotonin syndrome risk."),
    ("sertraline",  "diazepam",      "MODERATE","CNS depression may be enhanced."),
    ("digoxin",     "furosemide",    "MODERATE","Electrolyte imbalance may increase digoxin toxicity."),
    ("clopidogrel", "omeprazole",    "MODERATE","Omeprazole may reduce clopidogrel effectiveness."),
    ("ciprofloxacin","metformin",    "LOW",    "Monitor blood sugar — ciprofloxacin may alter glucose."),
    ("prednisolone","ibuprofen",     "HIGH",   "Increased risk of GI bleeding and ulcers."),
    ("prednisolone","metformin",     "MODERATE","Steroids can raise blood sugar, reducing metformin effect."),
    ("amlodipine",  "metoprolol",    "LOW",    "Additive blood pressure lowering — monitor BP."),
    ("lisinopril",  "furosemide",    "MODERATE","Risk of excessive blood pressure drop."),
    ("diazepam",    "fluoxetine",    "MODERATE","Fluoxetine may increase diazepam blood levels."),
]

# ── Emergency risk rules ──────────────────────────────────────────────────────
EMERGENCY_KEYWORDS = {
    "chest pain": 40, "shortness of breath": 35, "difficulty breathing": 35,
    "unconscious": 50, "not breathing": 50, "stroke": 45, "seizure": 45,
    "severe bleeding": 40, "coughing blood": 40, "vomiting blood": 40,
    "sudden vision loss": 35, "sudden numbness": 30, "severe headache": 25,
    "high fever": 20, "fever": 10, "dizziness": 10, "fainting": 25,
    "allergic reaction": 30, "swelling throat": 40, "rapid heartbeat": 20,
    "confusion": 25, "slurred speech": 35, "weakness": 15, "nausea": 5,
}

USERS = {"admin": "medsafe123", "doctor": "health2024", "user": "password"}
response_cache = {}
rate_limit_store = {}

def is_rate_limited(username):
    now = time.time()
    ts = [t for t in rate_limit_store.get(username, []) if now - t < 60]
    rate_limit_store[username] = ts
    if len(ts) >= 20:
        return True, int(60 - (now - ts[0]))
    ts.append(now)
    rate_limit_store[username] = ts
    return False, 0

def groq_ask(messages, stream=False, max_tokens=1024):
    return groq_client.chat.completions.create(
        model=MODEL, messages=messages, stream=stream,
        temperature=0.6, max_tokens=max_tokens
    )

def fuzzy_match_medicine(name):
    name = name.lower().strip()
    keys = list(MEDICINE_DB.keys())
    match, score = fuzz_process.extractOne(name, keys)
    if score >= 60:
        return match, MEDICINE_DB[match]
    return None, None

def check_interactions(med_list):
    matched = []
    for m in med_list:
        key, info = fuzzy_match_medicine(m)
        if key:
            matched.append({"input": m, "matched": key, "info": info})

    found = []
    keys = [m["matched"] for m in matched]
    for (a, b, severity, note) in KNOWN_INTERACTIONS:
        if a in keys and b in keys:
            found.append({"drug1": a, "drug2": b, "severity": severity, "note": note})
    return matched, found

def calc_risk_score(symptoms_text):
    text = symptoms_text.lower()
    score = 0
    triggered = []
    for kw, pts in EMERGENCY_KEYWORDS.items():
        if kw in text:
            score += pts
            triggered.append(kw)
    score = min(score, 100)
    if score >= 70:   level = ("CRITICAL", "🔴", "Call emergency services immediately (911/112).")
    elif score >= 45: level = ("HIGH",     "🟠", "Seek urgent medical care within the hour.")
    elif score >= 25: level = ("MODERATE", "🟡", "See a doctor today or visit urgent care.")
    elif score >= 10: level = ("LOW",      "🟢", "Monitor symptoms. See a doctor if they worsen.")
    else:             level = ("MINIMAL",  "⚪", "Symptoms appear mild. Rest and stay hydrated.")
    return score, level, triggered

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return redirect(url_for("dashboard") if "user" in session else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","").strip()
        if u in USERS and USERS[u] == p:
            session["user"] = u
            session["history"] = []
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("index.html", username=session["user"])

@app.route("/interaction")
def interaction():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("interaction.html", username=session["user"])

@app.route("/prescription")
def prescription():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("prescription.html", username=session["user"])

@app.route("/sideeffects")
def sideeffects():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("sideeffects.html", username=session["user"])

@app.route("/symptoms")
def symptoms():
    if "user" not in session: return redirect(url_for("login"))
    return render_template("symptoms.html", username=session["user"])

# ── API: Chat ─────────────────────────────────────────────────────────────────
CHAT_SYSTEM = """You are MedSafe AI, a professional medical safety assistant.
- Use **bold** for headers like **Possible Causes:**, **What To Do:**, **Warning Signs:**
- Use bullet points with - for lists
- Flag emergencies with ⚠️ EMERGENCY
- End with: *Disclaimer: For informational purposes only. Consult a licensed doctor.*"""

@app.route("/chat", methods=["POST"])
def chat():
    if "user" not in session: return jsonify({"error":"Unauthorized"}), 401
    limited, w = is_rate_limited(session["user"])
    if limited: return jsonify({"response": f"⚠️ Too fast. Wait {w}s."})
    data = request.get_json()
    msg = data.get("message","").strip()
    if not msg: return jsonify({"error":"Empty"}), 400
    if "history" not in session: session["history"] = []

    ck = hashlib.md5(msg.lower().encode()).hexdigest()
    if ck in response_cache: return jsonify({"response": response_cache[ck]})

    msgs = [{"role":"system","content":CHAT_SYSTEM}]
    for h in session["history"][-8:]: msgs.append({"role":h["role"],"content":h["content"]})
    msgs.append({"role":"user","content":msg})

    def generate():
        full = []
        try:
            for chunk in groq_ask(msgs, stream=True):
                d = chunk.choices[0].delta.content
                if d:
                    full.append(d)
                    yield f"data: {json.dumps({'chunk':d})}\n\n"
        except Exception as e:
            err = f"⚠️ Error: {e}"
            yield f"data: {json.dumps({'chunk':err})}\n\n"
            full.append(err)
        complete = "".join(full)
        if not complete.startswith("⚠️"): response_cache[ck] = complete
        session["history"].append({"role":"user","content":msg})
        session["history"].append({"role":"assistant","content":complete})
        session.modified = True
        yield f"data: {json.dumps({'done':True})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/clear", methods=["POST"])
def clear():
    session["history"] = []
    session.modified = True
    return jsonify({"status":"cleared"})

# ── API: Interaction Checker ──────────────────────────────────────────────────
@app.route("/api/interaction", methods=["POST"])
def api_interaction():
    if "user" not in session: return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json()
    raw = data.get("medicines","")
    med_list = [m.strip() for m in re.split(r"[,\n]+", raw) if m.strip()]
    if len(med_list) < 2:
        return jsonify({"error":"Enter at least 2 medicines."}), 400

    matched, interactions = check_interactions(med_list)

    # AI summary
    interaction_text = "\n".join([f"- {i['drug1']} + {i['drug2']} ({i['severity']}): {i['note']}" for i in interactions]) or "No known interactions found in database."
    med_names = ", ".join([m["matched"] for m in matched])
    prompt = f"""You are MedSafe AI. A patient is taking: {med_names}.
Known interactions detected:
{interaction_text}

Write a short, clear safety summary (3-5 sentences) in simple language. Mention any high-risk combinations first. End with advice to consult their doctor. Do NOT diagnose."""

    try:
        resp = groq_ask([{"role":"user","content":prompt}], max_tokens=400)
        ai_note = resp.choices[0].message.content
    except Exception as e:
        ai_note = f"Could not generate AI summary: {e}"

    return jsonify({"matched": matched, "interactions": interactions, "ai_note": ai_note})

# ── API: Prescription OCR ─────────────────────────────────────────────────────
@app.route("/api/prescription", methods=["POST"])
def api_prescription():
    if "user" not in session: return jsonify({"error":"Unauthorized"}), 401

    image_data = None
    if "file" in request.files:
        f = request.files["file"]
        image_data = base64.b64encode(f.read()).decode("utf-8")
        mime = f.content_type or "image/jpeg"
    elif request.is_json:
        body = request.get_json()
        image_data = body.get("image_b64")
        mime = body.get("mime","image/jpeg")

    if not image_data:
        return jsonify({"error":"No image provided"}), 400

    prompt = """You are a medical prescription reader. Analyze this prescription image and extract:
1. All medicine names mentioned
2. Their likely active ingredient / salt (if identifiable)
3. Dosage if visible
4. Frequency if visible

Return ONLY a valid JSON array like:
[
  {"medicine": "Amoxicillin 500mg", "salt": "Amoxicillin Trihydrate", "dosage": "500mg", "frequency": "3x daily"},
  ...
]
If you cannot read something clearly, use "unclear" as the value. Return only the JSON array, nothing else."""

    try:
        resp = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_data}"}}
                ]
            }],
            max_tokens=600
        )
        raw = resp.choices[0].message.content.strip()
        # Extract JSON from response
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        medicines = json.loads(match.group()) if match else []
    except Exception as e:
        return jsonify({"error": f"Could not process image: {str(e)}"}), 500

    # Cross-check each extracted medicine against our DB
    enriched = []
    for item in medicines:
        key, info = fuzzy_match_medicine(item.get("medicine",""))
        item["db_match"] = key
        item["db_info"] = info
        enriched.append(item)

    return jsonify({"medicines": enriched})

# ── API: Side Effect Monitor ──────────────────────────────────────────────────
@app.route("/api/sideeffects", methods=["POST"])
def api_sideeffects():
    if "user" not in session: return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json()
    age      = data.get("age","")
    gender   = data.get("gender","")
    medicines= data.get("medicines","")
    dosage   = data.get("dosage","")
    experience = data.get("experience","")

    prompt = f"""You are MedSafe AI. A patient has reported the following:
- Age: {age}, Gender: {gender}
- Medicines taken: {medicines}
- Dosage: {dosage}
- Post-medication experience: {experience}

Provide a short educational response (4-6 sentences) that:
1. Acknowledges their experience
2. Lists 2-3 possible contributing factors (not a diagnosis)
3. States ONE clear precaution to watch for
4. Advises when to seek medical help

Keep the tone calm, informative, and non-alarming. End with a disclaimer."""

    try:
        resp = groq_ask([{"role":"user","content":prompt}], max_tokens=500)
        result = resp.choices[0].message.content
    except Exception as e:
        result = f"⚠️ Error: {e}"

    return jsonify({"response": result})

# ── API: Symptom Solver + Risk Score ─────────────────────────────────────────
@app.route("/api/symptoms", methods=["POST"])
def api_symptoms():
    if "user" not in session: return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json()
    symptoms_text = data.get("symptoms","").strip()
    age    = data.get("age","")
    gender = data.get("gender","")

    if not symptoms_text:
        return jsonify({"error":"Please describe your symptoms."}), 400

    # Risk score
    score, (level, emoji, action), triggered = calc_risk_score(symptoms_text)

    prompt = f"""You are MedSafe AI. A {age} year old {gender} reports: "{symptoms_text}"

Provide structured guidance with these sections:
**Possible Causes:** (2-3 likely causes, not a diagnosis)
**Home Remedies & Lifestyle:** (practical tips)
**Dietary Tips:** (what to eat/avoid)
**Breathing / Relaxation:** (if relevant, suggest a simple exercise)
**Warning Signs to Watch:** (when to seek immediate help)

Keep it clear, practical, and educational. End with a disclaimer."""

    try:
        resp = groq_ask([{"role":"user","content":prompt}], max_tokens=700)
        guidance = resp.choices[0].message.content
    except Exception as e:
        guidance = f"⚠️ Error: {e}"

    return jsonify({
        "guidance": guidance,
        "risk_score": score,
        "risk_level": level,
        "risk_emoji": emoji,
        "risk_action": action,
        "triggered_keywords": triggered
    })

if __name__ == "__main__":
    app.run(debug=True)
