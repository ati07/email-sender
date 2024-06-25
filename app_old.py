import os
import base64
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock
import time
import random
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
socketio = SocketIO(app)

# SCOPES for Gmail API
SCOPES = ['https://www.googleapis.com/auth/gmail.send']

DAILY_LIMIT = 2000  # Daily email sending limit per Gmail account
UPLOAD_EMAIL_FOLDER = 'upload_email'
UPLOAD_CONTENT_FOLDER = 'upload_content'
CREDENTIALS_FOLDER = 'credentials'

if not os.path.exists(UPLOAD_EMAIL_FOLDER):
    os.makedirs(UPLOAD_EMAIL_FOLDER)
if not os.path.exists(UPLOAD_CONTENT_FOLDER):
    os.makedirs(UPLOAD_CONTENT_FOLDER)

# Initialize progress_info dictionary and lock
progress_info = {
    "total_count": 0,
    "sent_count": 0,
    "percent_complete": 0,
    "statuses": []
}
progress_info_lock = Lock()  # Lock object for thread safety

def authenticate_gmail(credentials_filename):
    """Authenticate the user and return the Gmail service object."""
    creds = None
    token_filename = f'token/token_{credentials_filename}'
    credentials_filepath = f'{CREDENTIALS_FOLDER}/{credentials_filename}'
    
    # The file token_<credentials_filename>.json stores the user's access and refresh tokens
    if os.path.exists(token_filename):
        creds = Credentials.from_authorized_user_file(token_filename, SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_filepath, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(token_filename, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def create_message(to, subject, body_text, body_html):
    """Create a message for an email."""
    message = MIMEMultipart('alternative')
    message['to'] = to
    message['subject'] = subject
    
    # Attach both plain text and HTML versions of the message
    part1 = MIMEText(body_text, 'plain')
    part2 = MIMEText(body_html, 'html')
    
    message.attach(part1)
    message.attach(part2)
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {'raw': raw}

def send_email(service, user_id, message):
    """Send an email message."""
    try:
        message = (service.users().messages().send(userId=user_id, body=message).execute())
        return message
    except Exception as error:
        raise error

def is_valid_email(email):
    """Check if the email address is valid."""
    email_regex = re.compile(r"[^@]+@[^@]+\.[^@]+")
    return re.match(email_regex, email) is not None

def send_email_with_credential(credentials_filename, recipient, subject, body_html, email_count, retry_count=0):
    """Send an email using a specific credential and update progress info."""
    service = authenticate_gmail(credentials_filename)
    body_text = "This is a test email."  # Plain text body for fallback
    message = create_message(recipient, subject, body_text, body_html)
    try:
        send_email(service, 'me', message)
        with open('success.txt', 'a') as success_file:
            success_file.write(f"Email sent to {recipient} successfully using {credentials_filename}.\n")
        with progress_info_lock:
            progress_info["sent_count"] += 1
            email_count[credentials_filename] += 1
            progress_info["percent_complete"] = (progress_info["sent_count"] / progress_info["total_count"]) * 100
            progress_info["statuses"].append({"recipient": recipient, "status": "Sent"})
        socketio.emit('progress_update', {
            "percent_complete": progress_info["percent_complete"],
            "statuses": progress_info["statuses"],
            "total_count": progress_info["total_count"],
            "sent_count": progress_info["sent_count"]
        })
    except Exception as error:
        if 'User-rate limit exceeded' in str(error) or 'You have reached a limit for sending mail' in str(error):
            retry_count += 1
            if retry_count <= 5:  # Retry up to 5 times with exponential backoff
                sleep_time = (2 ** retry_count) + random.uniform(0, 1)
                time.sleep(sleep_time)
                send_email_with_credential(credentials_filename, recipient, subject, body_html, email_count, retry_count)
            else:
                with open('error.txt', 'a') as error_file:
                    error_file.write(f"Failed to send email to {recipient} using {credentials_filename} after retries. Error: {error}\n")
                with progress_info_lock:
                    progress_info["statuses"].append({"recipient": recipient, "status": f"Failed after retries. Error: {error}"})
                socketio.emit('progress_update', {
                    "percent_complete": progress_info["percent_complete"],
                    "statuses": progress_info["statuses"]
                })
        else:
            with open('error.txt', 'a') as error_file:
                error_file.write(f"Failed to send email to {recipient} using {credentials_filename}. Error: {error}\n")
            with progress_info_lock:
                progress_info["statuses"].append({"recipient": recipient, "status": f"Failed. Error: {error}"})
            socketio.emit('progress_update', {
                "percent_complete": progress_info["percent_complete"],
                "statuses": progress_info["statuses"]
            })

def rotate_credentials_and_send_emails(credentials_folder, recipients_info):
    """Rotate through credentials, sending emails in parallel."""
    # Get list of credentials filenames
    credentials_filenames = [f for f in os.listdir(credentials_folder) if f.endswith('.json')]

    progress_info["total_count"] = len(recipients_info)
    progress_info["sent_count"] = 0
    progress_info["percent_complete"] = 0
    progress_info["statuses"] = []

    email_count = {credential: 0 for credential in credentials_filenames}

    def send_batch(batch_recipients, credentials_filenames):
        """Send a batch of emails using the available credentials in parallel."""
        futures = []
        with ThreadPoolExecutor(max_workers=len(credentials_filenames)) as executor:
            for i, (recipient, subject, body_html) in enumerate(batch_recipients):
                credential = credentials_filenames[i % len(credentials_filenames)]
                if email_count[credential] < DAILY_LIMIT:
                    futures.append(executor.submit(send_email_with_credential, credential, recipient, subject, body_html, email_count))
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"An error occurred while sending an email: {exc}")

    # Calculate batch size
    batch_size = min(50, DAILY_LIMIT * len(credentials_filenames))  # 50 emails at a time or as much as daily limit allows

    # Process recipients in batches
    for i in range(0, len(recipients_info), batch_size):
        batch_recipients = recipients_info[i:i + batch_size]
        send_batch(batch_recipients, credentials_filenames)
        time.sleep(1)  # Sleep for 1 second to respect the rate limit

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload_emails', methods=['POST'])
def upload_emails():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file:
        filepath = os.path.join(UPLOAD_EMAIL_FOLDER, 'recipients.txt')
        file.save(filepath)
        return jsonify({"message": "Email list uploaded successfully"}), 200

@app.route('/upload_content', methods=['POST'])
def upload_content():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if file:
        filepath = os.path.join(UPLOAD_CONTENT_FOLDER, file.filename)
        file.save(filepath)
        return jsonify({"message": f"Content file {file.filename} uploaded successfully"}), 200

@app.route('/send_emails', methods=['POST'])
def send_emails():
    try:
        with open(os.path.join(UPLOAD_EMAIL_FOLDER, 'recipients.txt'), 'r') as file:
            recipients_data = file.readlines()
    except FileNotFoundError:
        return jsonify({"error": "Recipients list not found"}), 400

    recipients_info = []
    for line in recipients_data:
        email, subject, html_filename = line.strip().split(',')
        if is_valid_email(email):
            try:
                with open(os.path.join(UPLOAD_CONTENT_FOLDER, html_filename.replace('\r', '')), 'r') as html_file:
                    html_content = html_file.read()
                recipients_info.append((email, subject, html_content))
            except FileNotFoundError:
                return jsonify({"error": f"HTML content file {html_filename} not found"}), 400
    
    # Start sending emails in the background
    ThreadPoolExecutor().submit(rotate_credentials_and_send_emails, CREDENTIALS_FOLDER, recipients_info)
    return jsonify({"message": "Email sending started"}), 200

if __name__ == '__main__':
    socketio.run(app, debug=True)
