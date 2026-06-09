"""app.py — WeaselOMeter server.

Serves the static front-end (index.html, editor.html, WeaselChap.png) and a
small read/write API for the word bank (weaselwords.json).

Persistence: a single JSON file under data/ (volume-mounted in Docker). Writes
are atomic (tmp + os.replace), schema-validated, optimistic-locked via ETag, and
backed up on every save.

Auth: Cloudflare Access sits in front (same pattern as ARMR). Reads are open
(the whole site is behind CF Access anyway). The write endpoint additionally
checks the caller's CF-Access email is in EDITOR_EMAILS / ADMIN_EMAILS.
When Cloudflare Access is not configured (local dev), writes are allowed.
"""
import os, json, hashlib, shutil, datetime, tempfile, logging

from flask import Flask, request, jsonify, send_from_directory, Response, abort

from cf_auth import verify_cf_jwt, is_enabled as cf_enabled

APP_VERSION = "0.1.0"

log = logging.getLogger("weaselometer")
logging.basicConfig(level=logging.INFO)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "weaselwords.json")
BACKUPS   = os.path.join(DATA_DIR, "backups")
SEED_FILE = os.path.join(BASE_DIR, "weaselwords.json")  # git-tracked seed

# front-end assets that may be served from the project root
ASSETS = {"index.html", "editor.html", "WeaselChap.png"}


# ── data helpers ────────────────────────────────────────────────────────────
def ensure_data():
    """Create data/ and seed weaselwords.json from the repo copy on first run."""
    os.makedirs(BACKUPS, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        src = SEED_FILE if os.path.exists(SEED_FILE) else None
        if src:
            shutil.copy2(src, DATA_FILE)
            log.info("Seeded %s from %s", DATA_FILE, src)
        else:
            with open(DATA_FILE, "w", encoding="utf-8") as fh:
                json.dump({"version": "0.0.0", "categories": {}, "tiers": [],
                           "entries": [], "tlaOverrides": [], "calibration": {},
                           "meta": {}}, fh, indent=2)


def read_raw() -> str:
    with open(DATA_FILE, "r", encoding="utf-8") as fh:
        return fh.read()


def etag_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def atomic_write(text: str):
    """Write text to DATA_FILE atomically (tmp file on same dir + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".ww-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, DATA_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def backup(by: str):
    """Copy the current data file into backups/ with a timestamp + editor tag."""
    if not os.path.exists(DATA_FILE):
        return
    ts  = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    who = "".join(c for c in (by or "anon") if c.isalnum() or c in "._-@") or "anon"
    shutil.copy2(DATA_FILE, os.path.join(BACKUPS, f"weaselwords-{ts}-{who}.json"))
    # keep the most recent 50 backups
    files = sorted(os.listdir(BACKUPS))
    for old in files[:-50]:
        try:
            os.remove(os.path.join(BACKUPS, old))
        except OSError:
            pass


# ── schema validation (server-side guard) ───────────────────────────────────
def validate(d) -> list:
    """Return a list of error strings; empty means valid."""
    errs = []
    if not isinstance(d, dict):
        return ["payload is not a JSON object"]
    cats = d.get("categories")
    if not isinstance(cats, dict) or not cats:
        errs.append("categories must be a non-empty object")
        cats = cats if isinstance(cats, dict) else {}
    if not isinstance(d.get("tiers"), list) or not d.get("tiers"):
        errs.append("tiers must be a non-empty array")
    entries = d.get("entries")
    if not isinstance(entries, list):
        errs.append("entries must be an array")
        entries = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            errs.append(f"entry #{i+1} is not an object"); continue
        if e.get("category") not in cats:
            errs.append(f"entry #{i+1}: unknown category '{e.get('category')}'")
        m = e.get("match")
        if not isinstance(m, list) or not m or not all(isinstance(x, str) and x.strip() for x in m):
            errs.append(f"entry #{i+1}: match must be a non-empty array of strings")
        sc = e.get("score")
        default = cats.get(e.get("category"), {}).get("defaultScore")
        if sc is None and default is None:
            errs.append(f"entry #{i+1}: no score and category has no defaultScore")
        elif sc is not None and not isinstance(sc, (int, float)):
            errs.append(f"entry #{i+1}: score must be a number")
    if isinstance(d.get("tlaOverrides"), list):
        for i, o in enumerate(d["tlaOverrides"]):
            if not isinstance(o, dict) or not isinstance(o.get("match"), list):
                errs.append(f"tlaOverride #{i+1}: match must be an array")
    return errs


# ── auth helper ─────────────────────────────────────────────────────────────
def _allowed_editors() -> set:
    raw = os.environ.get("EDITOR_EMAILS", "") or os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def current_editor():
    """Return (ok, email). ok=True if the caller may write.
    Local dev (CF disabled) -> allowed as 'local'. Otherwise the CF Access email
    must be in EDITOR_EMAILS/ADMIN_EMAILS (or that list is empty = allow any
    authenticated CF user)."""
    if not cf_enabled():
        return True, "local"
    token = (request.headers.get("Cf-Access-Jwt-Assertion")
             or request.cookies.get("CF_Authorization"))
    email = verify_cf_jwt(token) if token else None
    if not email:
        return False, None
    allow = _allowed_editors()
    if allow and email.lower() not in allow:
        return False, email
    return True, email


# ── app ──────────────────────────────────────────────────────────────────────
def create_app():
    app = Flask(__name__, static_folder=None)
    ensure_data()

    @app.route("/")
    def home():
        return send_from_directory(BASE_DIR, "index.html")

    @app.route("/editor")
    @app.route("/editor.html")
    def editor():
        return send_from_directory(BASE_DIR, "editor.html")

    @app.route("/weaselwords.json")
    def words_file():
        """Raw word bank — what the app's front-end fetches. Always fresh."""
        resp = Response(read_raw(), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["ETag"] = etag_of(read_raw())
        return resp

    @app.route("/api/weaselwords", methods=["GET"])
    def api_get():
        raw = read_raw()
        return jsonify({"data": json.loads(raw), "etag": etag_of(raw),
                        "version": APP_VERSION, "cf": cf_enabled()})

    @app.route("/api/weaselwords", methods=["POST"])
    def api_post():
        ok, email = current_editor()
        if not ok:
            return jsonify({"error": "not authorised to edit",
                            "email": email}), 403

        body = request.get_json(silent=True) or {}
        data = body.get("data")
        if data is None:
            return jsonify({"error": "missing 'data'"}), 400

        errs = validate(data)
        if errs:
            return jsonify({"error": "validation failed", "details": errs}), 400

        # optimistic lock: reject if the file changed since the client loaded it
        current = read_raw()
        base_etag = body.get("etag")
        if base_etag and base_etag != etag_of(current):
            return jsonify({"error": "conflict",
                            "message": "The word bank changed since you loaded it. Reload and reapply.",
                            "currentEtag": etag_of(current)}), 409

        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        backup(email)
        atomic_write(text)
        log.info("weaselwords.json saved by %s (%d entries)",
                 email, len(data.get("entries", [])))
        return jsonify({"ok": True, "etag": etag_of(text),
                        "savedBy": email,
                        "at": datetime.datetime.utcnow().isoformat() + "Z"})

    @app.route("/api/health")
    def health():
        return jsonify({"ok": True, "version": APP_VERSION,
                        "cf_access": cf_enabled(),
                        "entries": len(json.loads(read_raw()).get("entries", []))})

    @app.route("/<path:fname>")
    def asset(fname):
        if fname in ASSETS or fname.rsplit("/", 1)[-1] in ASSETS:
            return send_from_directory(BASE_DIR, fname)
        abort(404)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_ENV") == "development")
