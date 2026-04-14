from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
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

if __name__ == '__main__':
    # Run the server
    app.run(debug=True, port=5000)