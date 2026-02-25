import os
import smtplib
from email.mime.text import MIMEText
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure dotenv works inside PyInstaller .exe
if getattr(sys, "frozen", False):
    # running in a bundle
    base_path = Path(getattr(sys, "_MEIPASS"))  # temporary folder where .env gets extracted
else:
    # running in normal Python
    base_path = Path(__file__).parent

dotenv_path = base_path / ".env"

load_dotenv(dotenv_path)

EMAIL_PASSWORD = str(os.getenv("EMAIL_PASSWORD"))
CC_RAW = os.getenv("CC", "") 
cc = [email.strip() for email in CC_RAW.split(",") if email.strip()]

def send_mail(query, jrny_id=None):
    subject = "Ghost failed to resolve address"

    body = f"- Ghost could not resolve the address: {query}"
    if jrny_id:
        body += f"\n - Journey ID: {jrny_id}"

    smtp_server = "secure.emailsrvr.com"
    smtp_port = 465
    user = "farakh@streetcars.co.uk"
    password = EMAIL_PASSWORD
    cc = [
        "nic@streetcars.co.uk",
        "transport@streetcars.co.uk",
        "accounts@goodwinsolympic.co.uk"
        ]
    
    to = "farakh@streetcars.co.uk"

    if not all([smtp_server, smtp_port, user, password, to]):
        print("Email not sent: missing email environment variables.")
        return

    # force consistent line breaks
    body = body.replace("\n", "\r\n")

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Cc"] = ", ".join(cc)

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(user, password)
            server.sendmail(user, [to] + cc, msg.as_string())
    except Exception as e:
        print(f"Email not sent: {e}")


if __name__ == "__main__":
    send_mail("Test query", "This is a test email please ignore.")
