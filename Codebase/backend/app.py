from flask import Flask, flash, redirect, request, jsonify, render_template, session, url_for
from flask_cors import CORS
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "..", "templates")
STATIC_DIR = os.path.join(BASE_DIR, "..", "static")

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "eventbook-dev-secret")
CORS(app)  # This allows your frontend to connect to the backend

# Mock database of events for your chatbot to "know" things
events_info = {
    "tech_summit": "The Tech Summit is on May 20th in Hall A.",
    "wedding_expo": "The Wedding Expo starts at 10 AM this Sunday."
}

@app.route('/chat', methods=['POST'])
def chat_endpoint():
    data = request.json
    user_message = data.get("message", "").lower()
    
    # Basic logic for your chatbot
    if "hello" in user_message or "hi" in user_message:
        reply = "Hello! I'm your Event Support Assistant. How can I help you today?"
    elif "event" in user_message:
        reply = "We have several events! Are you asking about the Tech Summit or Wedding Expo?"
    elif "tech summit" in user_message:
        reply = events_info["tech_summit"]
    else:
        reply = "I'm still learning! Can you please rephrase that?"

    return jsonify({
        "status": "success",
        "reply": reply
    })


@app.route("/")
def homepage():
    return render_template("customer/homepage.html")


@app.route("/login", methods=["GET", "POST"])
@app.route('/login/index', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please enter both email and password.", "error")
            return redirect(url_for("login"))

        session["authenticated_user"] = email
        flash("Login successful. Welcome to DashDesk!", "success")
        return redirect(url_for("booking_page"))

    return render_template('login/index.html')


@app.route("/register")
def register():
    return render_template("login/register.html")


@app.route("/login/alt")
def login_alt():
    return render_template("login/login.html")


@app.route("/login/customer")
def login_customer():
    return render_template("login/login_customer.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    return render_template("admin/dashboard.html")


@app.route("/admin/activechats")
def admin_activechats():
    return render_template("admin/activechats.html")


@app.route("/admin/booking")
def admin_booking():
    return render_template("admin/booking.html")


@app.route("/admin/customer")
def admin_customer():
    return render_template("admin/customer.html")

@app.route('/book')
@app.route('/customer/booking')
def booking_page():
    return render_template('customer/booking_customer.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route('/booking/confirm', methods=['POST'])
def confirm_booking():
    venue = request.form.get("venue", "").strip()

    if not venue:
        flash("Please select a venue package before checkout.", "error")
        return redirect(url_for("booking_page"))

    flash(f"Booking confirmed for {venue}.", "success")
    return redirect(url_for("booking_page"))

if __name__ == '__main__':
    # Run the server
    app.run(debug=True, port=5000)